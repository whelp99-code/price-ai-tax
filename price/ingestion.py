"""Leased source-reader jobs with fenced, atomic result publication."""
from __future__ import annotations
import hashlib
import json
import re
import time
from .db import DomainError, canonical, digest, now, uid
from .uploads import Uploads
from .source_tables import parse_source_table
from .source_workbooks import parse_source_workbook
from .native_documents import parse_text_document, parse_pdf_document


class Ingestion:
    def __init__(self, service):
        self.service, self.db, self.auth = service, service.db, service.auth
        self.uploads = Uploads(service)

    def claim(self, company=None, document_id=None):
        with self.db.transaction() as c:
            ts = int(time.time())
            extra = ''
            params = [ts]
            if company:
                extra += ' AND company_id=?'
                params.append(company)
            if document_id:
                extra += ' AND document_id=?'
                params.append(document_id)
            exhausted = c.execute("SELECT id,company_id,document_id FROM upload_jobs WHERE state='RUNNING' AND lease_until<? AND attempts>=3" + extra, params).fetchall()
            for job in exhausted:
                c.execute("UPDATE upload_jobs SET state='FAILED',error='RETRY_EXHAUSTED',lease_until=0,updated_at=? WHERE id=?", (now(), job['id']))
                c.execute("UPDATE upload_documents SET state='READ_FAILED',revision=revision+1,updated_at=? WHERE company_id=? AND id=?", (now(), job['company_id'], job['document_id']))
                self.db.audit(c, job['company_id'], 'system:source-reader', 'ingestion.exhausted', job['id'])
            row = c.execute("SELECT * FROM upload_jobs WHERE (state='QUEUED' OR (state='RUNNING' AND lease_until<?)) AND attempts<3" + extra + ' ORDER BY created_at,id LIMIT 1', params).fetchone()
            if not row:
                return None
            job = dict(row)
            token = uid('lease')
            c.execute("UPDATE upload_jobs SET state='RUNNING',attempts=attempts+1,lease_token=?,lease_until=?,updated_at=? WHERE id=?", (token, ts + 120, now(), job['id']))
            c.execute("UPDATE upload_documents SET state='PROCESSING',revision=revision+1,updated_at=? WHERE company_id=? AND id=?", (now(), job['company_id'], job['document_id']))
            job.update(lease_token=token, lease_until=ts + 120)
            return job

    def _ownership_conflicts(self, c, company, result):
        from .source_identity import ownership_conflicts
        facts = self.db.list(c, company, 'company_facts')
        return ownership_conflicts(facts[0] if facts else None, result)

    def finish(self, job, result):
        with self.db.transaction() as c:
            current = c.execute('SELECT * FROM upload_jobs WHERE id=? AND company_id=?', (job['id'], job['company_id'])).fetchone()
            if not current or current['state'] != 'RUNNING' or current['lease_token'] != job['lease_token'] or current['lease_until'] < int(time.time()):
                return {'processed': True, 'id': job['id'], 'state': 'LEASE_LOST', 'committed': False}
            active = c.execute("SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.company_id=? AND m.user_id=? AND m.role IN ('owner','preparer','reviewer') AND u.disabled=0", (job['company_id'], job['requested_by'])).fetchone()
            if not active:
                result = {'state': 'PERMISSION_REVIEW', 'records': [], 'issues': [{'code': 'UPLOAD_AUTHORIZATION_REVOKED'}]}
            conflicts = self._ownership_conflicts(c, job['company_id'], result) if result.get('state') == 'NORMALIZED' else []
            if conflicts:
                result = {**result, 'state': 'COMPANY_REVIEW', 'issues': conflicts + result.get('issues', [])}
            if result.get('state') not in ('NORMALIZED', 'VALIDATION_REQUIRED', 'FORMAT_REVIEW', 'READ_FAILED', 'PERMISSION_REVIEW', 'COMPANY_REVIEW', 'NO_ACTIVITY_REPORTED'):
                raise DomainError('INVALID_READER_RESULT', '자료 처리 결과 형식이 올바르지 않습니다.')
            document = c.execute('SELECT * FROM upload_documents WHERE company_id=? AND id=?', (job['company_id'], job['document_id'])).fetchone()
            records = result.get('records', [])
            from .extractions import generation_of, save_snapshot
            generation = generation_of(document)
            ids, case_ids, published = [], [], []
            for record in records:
                ident = 'source_' + digest({'document': document['id'], 'parser': result.get('parser_version'), 'row': record['row'], **({'sheet': record['sheet']} if record.get('sheet') else {}), **({'generation': generation} if generation > 1 else {})})
                body = {**record, 'document_id': document['id'], 'evidence_id': document['blob_id'], 'source_hash': document['sha256'],
                        'parser_version': result.get('parser_version'), 'validation': result['state'], 'generation': generation, 'lifecycle': 'CURRENT'}
                published.append(self.db.create(c, job['company_id'], 'source_record', body, ident=ident))
                ids.append(ident)
                if result['state'] == 'NORMALIZED' and record['kind'] not in ('company_registration','opening_balance'):
                    case_id = 'ingestion_' + digest({'source_record': ident})
                    case = {'source_record_id': ident, 'document_id': document['id'], 'evidence_id': document['blob_id'],
                            'state': 'WAITING_FOR_LINKING', 'reason': '자료를 읽었습니다. 다른 자료와의 연결 및 회계 처리가 필요합니다.',
                            'owner_question': None, 'accounting_complete': False, 'source_version': generation}
                    self.db.create(c, job['company_id'], 'ingestion_case', case, ident=case_id)
                    case_ids.append(case_id)
            summary = {k: v for k, v in result.items() if k != 'records'}
            summary.update(record_count=len(records), source_record_ids=ids, case_ids=case_ids, accounting_complete=False, generation=generation)
            save_snapshot(self.db, c, job['company_id'], document, summary, published)
            state = 'SUCCEEDED' if result['state'] == 'NORMALIZED' else 'BLOCKED'
            error = None if state == 'SUCCEEDED' else (result.get('issues') or [{'code': 'READ_REVIEW'}])[0]['code']
            c.execute('UPDATE upload_documents SET state=?,result=?,revision=revision+1,updated_at=? WHERE company_id=? AND id=?',
                      (result['state'], canonical(summary), now(), job['company_id'], job['document_id']))
            c.execute('UPDATE upload_jobs SET state=?,error=?,lease_until=0,updated_at=? WHERE id=? AND lease_token=?', (state, error, now(), job['id'], job['lease_token']))
            self.db.audit(c, job['company_id'], 'system:source-reader', 'ingestion.finished', job['document_id'],
                          {'state': result['state'], 'record_count': len(records), 'case_count': len(case_ids), 'parser': result.get('parser_version')})
            return {'processed': True, 'id': job['id'], 'state': state, 'error': error, 'records': len(records), 'cases': len(case_ids), 'committed': True}

    def run_once(self, company=None, document_id=None):
        job = self.claim(company, document_id)
        if not job:
            return {'processed': False}
        try:
            with self.db.connect() as c:
                row = c.execute('SELECT b.content,d.sha256,d.filename,d.mime FROM upload_documents d JOIN blobs b ON b.company_id=d.company_id AND b.id=d.blob_id WHERE d.company_id=? AND d.id=?', (job['company_id'], job['document_id'])).fetchone()
            if not row or hashlib.sha256(row['content']).hexdigest() != row['sha256']:
                raise DomainError('ORIGINAL_INTEGRITY', '원본 무결성 확인이 필요합니다.')
            if row['mime'] in ('text/csv', 'text/plain'):
                result = parse_text_document(row['content'])
            elif row['mime'] == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':
                result = parse_source_workbook(row['content'])
            elif row['mime'] == 'text/html':
                from .source_html import parse_source_html
                result = parse_source_html(row['content'])
            elif row['mime'] == 'application/xml':
                from .source_xml import parse_tax_invoice_xml
                result = parse_tax_invoice_xml(row['content'])
            elif row['mime'] == 'application/pdf':
                result = parse_pdf_document(row['content'], self.db.path.parent / 'reader-work')
            else:
                result = {'state': 'FORMAT_REVIEW', 'records': [], 'issues': [{'code': 'FORMAT_READER_REQUIRED'}]}
        except DomainError as exc:
            result = {'state': 'READ_FAILED', 'records': [], 'issues': [{'code': exc.code}]}
        except Exception:
            # Original source content and exception text never enter logs.
            result = {'state': 'READ_FAILED', 'records': [], 'issues': [{'code': 'READER_FAILED'}]}
        finished = self.finish(job, result)
        finished['document_id'] = job['document_id']
        return finished

    def records(self, actor, company, document_id, offset=0, limit=100):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise DomainError('PAGINATION', '자료 조회 범위가 올바르지 않습니다.', 422)
        with self.db.connect() as c:
            self.auth.guard(c, actor, company)
            if not c.execute('SELECT 1 FROM upload_documents WHERE company_id=? AND id=?', (company, document_id)).fetchone():
                raise DomainError('NOT_FOUND', '자료를 찾을 수 없습니다.', 404)
            rows = c.execute("SELECT * FROM objects WHERE company_id=? AND kind='source_record' AND json_extract(body,'$.document_id')=? AND COALESCE(json_extract(body,'$.lifecycle'),'CURRENT')='CURRENT' ORDER BY COALESCE(json_extract(body,'$.sheet'),''),CAST(json_extract(body,'$.row') AS INTEGER) LIMIT ? OFFSET ?", (company, document_id, limit, offset)).fetchall()
            total = c.execute("SELECT count(*) FROM objects WHERE company_id=? AND kind='source_record' AND json_extract(body,'$.document_id')=? AND COALESCE(json_extract(body,'$.lifecycle'),'CURRENT')='CURRENT'", (company, document_id)).fetchone()[0]
            return {'rows': [self.db.unpack(row) for row in rows], 'total': total, 'next_offset': offset + len(rows) if offset + len(rows) < total else None}
