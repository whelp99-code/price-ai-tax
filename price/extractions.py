"""Versioned extraction replay with original preservation and impact records.

Reprocessing is authenticated source reading, not ledger correction or approval.
An old extraction stays inspectable but cannot remain current while re-reading.
"""
from __future__ import annotations
import json
from typing import Annotated
from pydantic import Field, StrictInt, StrictStr, field_validator
from .db import DomainError, canonical, digest, now
from .upload_models import UploadInput
from .uploads import Uploads


class ReprocessInput(UploadInput):
    expected_revision: Annotated[StrictInt, Field(ge=1)]
    idempotency_key: Annotated[StrictStr, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$')]
    reason: Annotated[StrictStr, Field(min_length=1, max_length=500)]

    @field_validator('reason')
    @classmethod
    def reason_not_blank(cls, value):
        if not value.strip():
            raise ValueError('다시 읽는 사유가 필요합니다.')
        return value


def generation_of(document):
    result = json.loads(document['result']) if document['result'] else {}
    return int(result.get('generation', 1))


def source_records(db, c, company, document_id, *, active_only=True):
    rows = c.execute("SELECT * FROM objects WHERE company_id=? AND kind='source_record' AND json_extract(body,'$.document_id')=? ORDER BY COALESCE(json_extract(body,'$.sheet'),''),CAST(json_extract(body,'$.row') AS INTEGER)", (company, document_id)).fetchall()
    records = [db.unpack(r) for r in rows]
    return [r for r in records if not active_only or r.get('lifecycle', 'CURRENT') == 'CURRENT']


def save_snapshot(db, c, company, document, summary, records):
    generation = int(summary.get('generation', 1))
    ident = 'extraction_' + digest({'document': document['id'], 'generation': generation})
    existing = c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='extraction_run' AND id=?", (company, ident)).fetchone()
    if existing:
        return db.get(c, company, 'extraction_run', ident)
    prior = c.execute("SELECT * FROM objects WHERE company_id=? AND kind='extraction_run' AND json_extract(body,'$.document_id')=? AND json_extract(body,'$.generation')<? ORDER BY json_extract(body,'$.generation') DESC LIMIT 1", (company, document['id'], generation)).fetchone()
    previous = db.unpack(prior) if prior else None
    new_values = {str(r.get('sheet', '')) + ':' + str(r['row']): r['values'] for r in records}
    old_values = previous['record_values'] if previous else {}
    changes = []
    if previous:
        for row in sorted(set(new_values) | set(old_values)):
            old, new = old_values.get(row, {}), new_values.get(row, {})
            for field in sorted(set(old) | set(new)):
                if old.get(field) != new.get(field):
                    changes.append({'row_key': row, 'field': field, 'before': old.get(field), 'after': new.get(field)})
    snapshot = db.create(c, company, 'extraction_run', {
        'document_id': document['id'], 'generation': generation, 'original_sha256': document['sha256'],
        'summary': summary, 'record_values': new_values, 'record_ids': [r['id'] for r in records if 'id' in r],
        'value_changes': changes, 'accounting_complete': False, 'created_by': 'system:source-reader'}, ident=ident)
    if previous and changes:
        db.create(c, company, 'source_change_impact', {'document_id': document['id'], 'generation': generation,
                  'state': 'REVIEW_REQUIRED', 'reason': '재추출 값이 달라져 후속 거래·계산의 재검증이 필요합니다.',
                  'changed_fields': len(changes), 'snapshot_id': snapshot['id'], 'posted_journal_ids': [], 'accounting_changed': False})
    return snapshot


class Extractions:
    def __init__(self, service):
        self.service, self.db, self.auth = service, service.db, service.auth
        self.uploads = Uploads(service)

    def _document(self, c, company, ident):
        row = c.execute('SELECT * FROM upload_documents WHERE company_id=? AND id=?', (company, ident)).fetchone()
        if not row:
            raise DomainError('NOT_FOUND', '현재 회사의 원본 자료가 아닙니다.', 404)
        return dict(row)

    def reprocess(self, actor, company, ident, data: ReprocessInput):
        data = ReprocessInput.model_validate(data)
        key = 'reprocess_' + digest({'company': company, 'key': data.idempotency_key})
        request_hash = digest({'document_id': ident, **data.model_dump()})
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, ('owner', 'preparer', 'reviewer'))
            doc = self._document(c, company, ident)
            previous = c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='extraction_request' AND id=?", (company, key)).fetchone()
            if previous:
                saved = self.db.get(c, company, 'extraction_request', key)
                if saved['request_hash'] != request_hash:
                    raise DomainError('REPROCESS_KEY_CONFLICT', '같은 요청 번호에 다른 재처리 내용이 있습니다.')
                return {'id': ident, 'generation': saved['generation'], 'state': doc['state'], 'revision': doc['revision'], 'reused': True}
            if data.expected_revision != doc['revision']:
                raise DomainError('STALE_VERSION', '자료 상태가 바뀌었습니다. 다시 확인하세요.')
            job = c.execute('SELECT * FROM upload_jobs WHERE company_id=? AND document_id=?', (company, ident)).fetchone()
            if job and job['state'] in ('RUNNING', 'QUEUED'):
                raise DomainError('REPROCESS_BUSY', '이미 자료를 읽고 있습니다. 기존 작업 결과를 확인하세요.')
            inspection = json.loads(doc['inspection'])
            readable = inspection['state'] == 'STRUCTURE_CHECKED' or (inspection['state'] == 'READER_REQUIRED' and inspection['code'] in ('TABLE_READER_REQUIRED', 'DOCUMENT_READER_REQUIRED'))
            if not readable or not job:
                raise DomainError('DOCUMENT_NOT_REPROCESSABLE', '안전 검사 또는 지원 형식 확인이 먼저 필요합니다.')
            generation = generation_of(doc) + 1
            if generation > 100:
                raise DomainError('REPROCESS_LIMIT', '반복 재처리 한도를 확인해야 합니다.')
            prior_summary = json.loads(doc['result']) if doc['result'] else {}
            active_records = source_records(self.db, c, company, ident)
            if prior_summary and active_records:
                save_snapshot(self.db, c, company, doc, prior_summary, active_records)
            for record in active_records:
                self.db.replace(c, company, 'source_record', record['id'], {**record, 'lifecycle': 'SUPERSEDED', 'superseded_by_generation': generation}, record['revision'])
            for row in c.execute("SELECT * FROM objects WHERE company_id=? AND kind='ingestion_case' AND json_extract(body,'$.document_id')=?", (company, ident)).fetchall():
                case = self.db.unpack(row)
                if case.get('state') not in ('SUPERSEDED', 'CLOSED'):
                    self.db.replace(c, company, 'ingestion_case', case['id'], {**case, 'state': 'SUPERSEDED', 'superseded_by_generation': generation}, case['revision'])
            posted = [j['id'] for j in self.db.list(c, company, 'journal') if j.get('state') == 'POSTED' and doc['blob_id'] in j.get('evidence_ids', [])]
            if posted:
                self.db.create(c, company, 'source_change_impact', {'document_id': ident, 'generation': generation,
                               'state': 'REVIEW_REQUIRED', 'reason': '확정 장부가 참조하는 원본을 다시 읽습니다. 장부는 변경하지 않았습니다.',
                               'posted_journal_ids': posted, 'accounting_changed': False})
            pending = {'generation': generation, 'previous_generation': generation - 1, 'record_count': 0,
                       'source_record_ids': [], 'case_ids': [], 'issues': [], 'accounting_complete': False}
            c.execute("UPDATE upload_documents SET state='QUEUED',result=?,revision=revision+1,updated_at=? WHERE company_id=? AND id=?", (canonical(pending), now(), company, ident))
            c.execute("UPDATE upload_jobs SET state='QUEUED',requested_by=?,attempts=0,lease_token=NULL,lease_until=0,error=NULL,updated_at=? WHERE id=?", (actor.id, now(), job['id']))
            self.db.create(c, company, 'extraction_request', {'document_id': ident, 'generation': generation,
                           'request_hash': request_hash, 'requested_by': actor.id, 'reason': data.reason}, ident=key)
            self.db.audit(c, company, actor.id, 'source.reprocess_requested', ident,
                          {'generation': generation, 'reason': data.reason, 'retired_records': len(active_records), 'posted_impacts': len(posted)})
            return {'id': ident, 'generation': generation, 'revision': doc['revision'] + 1, 'state': 'QUEUED', 'reused': False}

    def history(self, actor, company, ident, offset=0, limit=20):
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 100:
            raise DomainError('INVALID_PAGE', '이력 조회 범위를 확인하세요.', 422)
        with self.db.connect() as c:
            self.auth.guard(c, actor, company)
            doc = self._document(c, company, ident)
            where = "company_id=? AND kind='extraction_run' AND json_extract(body,'$.document_id')=?"
            total = c.execute('SELECT count(*) FROM objects WHERE ' + where, (company, ident)).fetchone()[0]
            rows = c.execute("SELECT id,created_at,json_extract(body,'$.generation') AS generation,json_extract(body,'$.summary.state') AS state,json_extract(body,'$.summary.parser_version') AS parser_version,json_extract(body,'$.summary.record_count') AS record_count,json_array_length(body,'$.value_changes') AS change_count FROM objects WHERE " + where + " ORDER BY generation LIMIT ? OFFSET ?", (company, ident, limit, offset)).fetchall()
            runs = []
            for row in rows:
                changes = [json.loads(x[0]) for x in c.execute("SELECT j.value FROM objects o,json_each(o.body,'$.value_changes') j WHERE o.company_id=? AND o.kind='extraction_run' AND o.id=? LIMIT 100", (company, row['id']))]
                runs.append({'id': row['id'], 'generation': row['generation'], 'created_at': row['created_at'],
                             'record_count': row['record_count'] or 0,
                             'summary': {'state': row['state'], 'parser_version': row['parser_version']},
                             'value_changes': changes, 'value_change_count': row['change_count'] or 0,
                             'changes_truncated': (row['change_count'] or 0) > len(changes), 'accounting_complete': False})
            return {'document_id': ident, 'generation': generation_of(doc), 'runs': runs,
                    'total': total, 'offset': offset, 'next_offset': offset + len(runs) if offset + len(runs) < total else None,
                    'original_sha256': doc['sha256'], 'accounting_complete': False}

    def coverage(self, actor, company, ident):
        with self.db.connect() as c:
            self.auth.guard(c, actor, company)
            doc = self._document(c, company, ident)
            summary = json.loads(doc['result']) if doc['result'] else {}
            return {'document_id': ident, 'generation': generation_of(doc), 'state': doc['state'],
                    'source_rows': summary.get('source_rows'), 'record_count': summary.get('record_count', 0),
                    'controls': summary.get('controls', []), 'issues': summary.get('issues', []),
                    'control_total_present': bool(summary.get('control_total_present')),
                    'original_sha256': doc['sha256'], 'company_period_complete': False, 'accounting_complete': False}
