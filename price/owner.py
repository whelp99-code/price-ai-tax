"""Non-expert owner views and explicit factual onboarding, not tax approvals."""
from __future__ import annotations
import json
import re
from typing import Annotated, Literal
from pydantic import Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator
from .db import DomainError, digest, payload
from .upload_models import UploadInput


class InstrumentInput(UploadInput):
    kind: Literal['bank', 'card']
    identifier: Annotated[StrictStr, Field(pattern=r'^[0-9Xx* -]{6,40}$')]
    currency: Annotated[StrictStr, Field(pattern=r'^[A-Z]{3}$')] | None = None

    @field_validator('identifier')
    @classmethod
    def normalize_identifier(cls, value):
        value = re.sub(r'[ -]', '', value).upper()
        if not re.search(r'[0-9]', value):
            raise ValueError('식별 가능한 숫자가 필요합니다.')
        return value


class CompanyFactsInput(UploadInput):
    expected_revision: Annotated[StrictInt, Field(ge=0)]
    name: Annotated[StrictStr, Field(min_length=1, max_length=120)]
    business_number: Annotated[StrictStr, Field(pattern=r'^(?:[0-9]{10}|[0-9]{3}-[0-9]{2}-[0-9]{5})?$')] = ''
    mode: Literal['demo', 'company']
    instruments: Annotated[list[InstrumentInput], Field(max_length=30)] = []
    source_document_id: Annotated[StrictStr, Field(max_length=160)] | None = None
    confirmed: StrictBool

    @model_validator(mode='after')
    def valid_facts(self):
        if not self.confirmed or not self.name.strip():
            raise ValueError('화면에 표시된 회사 사실을 확인해야 합니다.')
        keys = [(i.kind, i.identifier) for i in self.instruments]
        if len(keys) != len(set(keys)):
            raise ValueError('같은 계좌 또는 카드가 반복되었습니다.')
        return self
class DocumentContextInput(UploadInput):
    expected_revision: Annotated[StrictInt, Field(ge=1)]
    expected_context_revision: Annotated[StrictInt, Field(ge=0)] = 0
    source_hash: Annotated[StrictStr, Field(pattern=r'^[a-f0-9]{64}$')]
    generation: Annotated[StrictInt, Field(ge=1)]
    identifier: Annotated[StrictStr, Field(pattern=r'^[0-9Xx* -]{6,40}$')]
    currency: Annotated[StrictStr, Field(pattern=r'^[A-Z]{3}$')]
    confirmed: StrictBool
    idempotency_key: Annotated[StrictStr, Field(pattern=r'^[A-Za-z0-9_-]{8,120}$')]

    @field_validator('identifier')
    @classmethod
    def normalize_identifier(cls, value):
        return InstrumentInput.normalize_identifier(value)

    @model_validator(mode='after')
    def must_confirm(self):
        if not self.confirmed:
            raise ValueError('이 문서의 계좌와 통화를 확인해야 합니다.')
        return self


class OwnerView:
    def __init__(self, service, uploads):
        self.service, self.db, self.auth, self.uploads = service, service.db, service.auth, uploads

    def _source_candidates(self, c, company):
        candidates, company_candidates = [], []
        for doc in c.execute('SELECT id,result FROM upload_documents WHERE company_id=? AND result IS NOT NULL ORDER BY created_at DESC LIMIT 50', (company,)):
            info = json.loads(doc['result']).get('metadata', {})
            if info.get('company_name') and info.get('business_number'):
                company_candidates.append({'name': info['company_name'], 'business_number': info['business_number'], 'source_document_id': doc['id'], 'legal_registry_verified': False})
            for kind, field in (('bank', 'account'), ('card', 'card')):
                if info.get(field):
                    value = re.sub(r'[ -]', '', info[field]).upper()
                    candidates.append({'kind': kind, 'identifier': value, 'currency': info.get('currency'),
                                       'source_document_id': doc['id'], 'ownership_verified': False})
        return candidates, company_candidates

    def onboarding(self, actor, company):
        with self.db.connect() as c:
            role = self.auth.guard(c, actor, company)
            row = c.execute('SELECT body FROM companies WHERE id=?', (company,)).fetchone()
            profile = json.loads(row[0])
            facts = self.db.list(c, company, 'company_facts')
            current = facts[0] if facts else None
            candidates, company_candidates = self._source_candidates(c, company)
            return {'mode': 'demo' if profile.get('demo') else 'company', 'profile_name': profile['name'],
                    'facts': current, 'revision': current['revision'] if current else 0,
                    'instrument_candidates': candidates, 'company_candidates': company_candidates, 'needs_confirmation': not bool(current),
                    'notice': '회사와 계좌의 사실 확인입니다. 회계·세무 처리를 승인하는 절차가 아닙니다.'}

    def confirm(self, actor, company, data: CompanyFactsInput):
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, ('owner',))
            profile = json.loads(c.execute('SELECT body FROM companies WHERE id=?', (company,)).fetchone()[0])
            expected_mode = 'demo' if profile.get('demo') else 'company'
            if data.mode != expected_mode:
                raise DomainError('COMPANY_MODE_MISMATCH', '데모 자료와 실제 회사 자료를 같은 공간으로 전환하지 않습니다.')
            old = self.db.list(c, company, 'company_facts')
            current = old[0] if old else None
            if data.expected_revision != (current['revision'] if current else 0):
                raise DomainError('STALE_VERSION', '회사 정보가 바뀌었습니다. 현재 내용을 다시 확인하세요.')
            number = data.business_number.replace('-', '')
            if current and current.get('business_number') and number != current['business_number']:
                if any(j.get('state') == 'POSTED' for j in self.db.list(c, company, 'journal')):
                    raise DomainError('COMPANY_IDENTITY_LOCKED', '확정 장부가 있는 회사의 식별 정보 변경은 별도 이관 검토가 필요합니다.')
            if data.source_document_id:
                if not c.execute('SELECT 1 FROM upload_documents WHERE company_id=? AND id=?', (company, data.source_document_id)).fetchone():
                    raise DomainError('NOT_FOUND', '현재 회사의 원본 자료가 아닙니다.', 404)
            values = data.model_dump(exclude={'expected_revision', 'confirmed'})
            values.update(business_number=number, confirmed_by=actor.id, verification='owner_factual_statement', legal_registry_verified=False)
            result = self.db.replace(c, company, 'company_facts', current['id'], values, current['revision']) if current else self.db.create(c, company, 'company_facts', values)
            self.db.audit(c, company, actor.id, 'company.facts_confirmed', result['id'], {'revision': result['revision'], 'source_document_id': data.source_document_id})
            from .source_identity import hold_sources_on_fact_change
            from .casework import Casework
            affected = hold_sources_on_fact_change(self.db, c, company, result, actor.id)
            work = Casework(self.service)
            before = {event['id']: event for event in self.db.list(c, company, 'economic_event') if event.get('lifecycle') == 'CURRENT'}
            prior_facts = [self.db.unpack(row) for row in c.execute("SELECT * FROM objects WHERE company_id=? AND kind='event_fact' AND COALESCE(json_extract(body,'$.active'),1)=1", (company,))]
            c.execute('SAVEPOINT casework_resume')
            try:
                linked = work._refresh(c, company, actor.id)
                current_events = {event['id']: event for event in self.db.list(c, company, 'economic_event') if event.get('lifecycle') == 'CURRENT'}
                for fact in prior_facts:
                    event = current_events.get(fact.get('event_id'))
                    old = before.get(fact.get('event_id'))
                    if not event or not old or event.get('issues') or fact.get('context_hash') != old.get('context_hash') or old.get('source_record_ids') != event.get('source_record_ids') or fact.get('context_hash') == event.get('context_hash'):
                        continue
                    self.db.create(c, company, 'event_fact', {**payload(fact), 'context_hash': event['context_hash']})
                c.execute('RELEASE SAVEPOINT casework_resume')
            except DomainError as error:
                c.execute('ROLLBACK TO SAVEPOINT casework_resume')
                c.execute('RELEASE SAVEPOINT casework_resume')
                resume = {'state': 'waiting' if error.code == 'CASEWORK_LIMIT' else 'error', 'updated': False, 'code': error.code, 'message': error.message}
                return {**result, 'source_context_held_ids': affected, 'casework_resume': resume}
            resume = {'state': 'waiting', 'updated': False, 'message': '나중에 확인된 회사 정보와 원본이 맞지 않아 거래 연결을 보류합니다.'} if affected else {'state': 'ready', 'updated': bool(linked.get('updated'))}
            return {**result, 'source_context_held_ids': affected, 'casework': linked, 'casework_resume': resume}
    def _document_records(self, c, company, document_id):
        return [self.db.unpack(row) for row in c.execute("SELECT * FROM objects WHERE company_id=? AND kind='source_record' AND json_extract(body,'$.document_id')=? AND COALESCE(json_extract(body,'$.lifecycle'),'CURRENT')='CURRENT'", (company, document_id))]

    def _confirmed_bank_candidates(self, facts):
        from .source_identity import confirmed_bank_identifiers
        return [{'kind': 'bank', 'identifier': ident, 'currency': None, 'instrument_currency_applied': False} for ident in sorted(confirmed_bank_identifiers(facts))]

    def _context_actions(self, c, company, only_ids=None):
        from .source_identity import context_is_usable, missing_bank_fields, normalize_identifier
        facts = self.db.list(c, company, 'company_facts')
        current = facts[0] if facts else None
        candidates = self._confirmed_bank_candidates(current)
        contexts = {item['document_id']: item for item in self.db.list(c, company, 'document_source_context')}
        if only_ids is not None:
            wanted = sorted(only_ids)
            if not wanted:
                return [], {}
            marks = ','.join('?' for _ in wanted)
            rows = c.execute(f'SELECT id,filename,state,revision,sha256,result FROM upload_documents WHERE company_id=? AND id IN ({marks})', (company, *wanted)).fetchall()
        else:
            rows = c.execute('SELECT id,filename,state,revision,sha256,result FROM upload_documents WHERE company_id=? ORDER BY created_at DESC LIMIT 200', (company,)).fetchall()
        actions, details = [], {}
        for row in rows:
            result = json.loads(row['result']) if row['result'] else {}
            generation = int(result.get('generation') or 1)
            records = self._document_records(c, company, row['id']) if row['state'] == 'NORMALIZED' else []
            missing = missing_bank_fields(records)
            stored = contexts.get(row['id'])
            usable = context_is_usable(stored, row['sha256'], generation, records) if records else False
            owner_context = None
            if usable:
                owner_context = {'id': stored['id'], 'revision': stored['revision'], 'identifier': stored['identifier'], 'currency': stored['currency'],
                                 'basis': 'owner_factual_statement', 'currency_origin': 'owner_statement', 'instrument_currency_applied': False,
                                 'original_preserved': True, 'confirmed_by': stored.get('confirmed_by'), 'source_hash': stored['source_hash'], 'generation': stored['generation']}
            action = None
            if row['state'] == 'NORMALIZED' and missing and not usable:
                source_accounts = sorted({normalize_identifier((record.get('values') or {}).get('account')) for record in records if record.get('kind') == 'bank_statement' and normalize_identifier((record.get('values') or {}).get('account'))})
                offered = candidates
                if len(source_accounts) == 1 and not any(ch in source_accounts[0] for ch in ('*', 'X')):
                    offered = [item for item in candidates if item['identifier'] == source_accounts[0]]
                elif source_accounts:
                    offered = []
                action = {'kind': 'confirm_document_context', 'label': '이 자료의 원본에 계좌 또는 통화가 없습니다. 확인된 회사 계좌는 후보일 뿐이며, 이 문서의 계좌와 통화를 직접 확인해야 합니다.',
                          'document_id': row['id'], 'filename': row['filename'], 'expected_revision': row['revision'],
                          'expected_context_revision': stored['revision'] if stored else 0, 'source_hash': row['sha256'], 'generation': generation,
                          'missing': missing, 'candidates': offered, 'reader_state': row['state'], 'reader_ready': True,
                          'refresh_skipped_evidence': False, 'instrument_currency_applied': False}
            details[row['id']] = {'reader_ready': row['state'] == 'NORMALIZED', 'missing_source_context': action, 'owner_context': owner_context}
            if action:
                actions.append(action)
        return actions, details

    def source_context_attention(self, actor, company, document_ids=None):
        with self.db.connect() as c:
            self.auth.guard(c, actor, company)
            actions, _details = self._context_actions(c, company, set(document_ids) if document_ids is not None else None)
            return actions

    def _context_response(self, saved):
        return {'id': saved['id'], 'revision': saved['revision'], 'document_id': saved['document_id'], 'identifier': saved['identifier'],
                'currency': saved['currency'], 'basis': saved['basis'], 'currency_origin': saved['currency_origin'],
                'instrument_currency_applied': False, 'applied_to_original': False, 'accounting_complete': False, 'active': bool(saved.get('active', True))}

    def _resume_context(self, c, company, actor_id):
        from .casework import Casework
        work = Casework(self.service)
        c.execute('SAVEPOINT document_context_resume')
        try:
            linked = work._refresh(c, company, actor_id)
            c.execute('RELEASE SAVEPOINT document_context_resume')
        except DomainError as error:
            c.execute('ROLLBACK TO SAVEPOINT document_context_resume')
            c.execute('RELEASE SAVEPOINT document_context_resume')
            return {'casework_resume': {'state': 'waiting' if error.code == 'CASEWORK_LIMIT' else 'error', 'updated': False, 'code': error.code, 'message': error.message}, 'accounting_complete': False}
        return {'casework': linked, 'casework_resume': {'state': 'ready', 'updated': bool(linked.get('updated'))}, 'accounting_complete': False}

    def confirm_document_context(self, actor, company, document_id, data: DocumentContextInput):
        data = DocumentContextInput.model_validate(data)
        from .source_identity import confirmed_bank_identifiers, context_conflicts, krw_fraction_fields, masked_identifier, missing_bank_fields
        key = 'docctxreq_' + digest({'company': company, 'key': data.idempotency_key})
        request_hash = digest({'document': document_id, **data.model_dump()})
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, ('owner',))
            previous = c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='document_context_request' AND id=?", (company, key)).fetchone()
            if previous:
                old = self.db.get(c, company, 'document_context_request', key)
                if old['request_hash'] != request_hash or old['actor'] != actor.id:
                    raise DomainError('DOCUMENT_CONTEXT_KEY_CONFLICT', '같은 요청 번호에 다른 문서 확인이 있습니다.')
                saved = self.db.get(c, company, 'document_source_context', old['confirmation_id'])
                return {**self._context_response(saved), 'reused': True, **self._resume_context(c, company, actor.id)}
            document = c.execute('SELECT * FROM upload_documents WHERE company_id=? AND id=?', (company, document_id)).fetchone()
            if not document:
                raise DomainError('NOT_FOUND', '현재 회사의 원본 자료가 아닙니다.', 404)
            if data.expected_revision != document['revision']:
                raise DomainError('STALE_VERSION', '자료 상태가 바뀌었습니다. 현재 문서를 다시 확인하세요.')
            if data.source_hash != document['sha256']:
                raise DomainError('STALE_SOURCE', '원본 해시가 현재 문서와 다릅니다. 이전 확인은 적용하지 않습니다.')
            result = json.loads(document['result']) if document['result'] else {}
            generation = int(result.get('generation') or 1)
            if data.generation != generation:
                raise DomainError('STALE_GENERATION', '원본 세대가 바뀌었습니다. 이전 확인은 적용하지 않습니다.')
            if document['state'] != 'NORMALIZED':
                raise DomainError('READER_NOT_READY', '자료 읽기가 끝나기 전의 상태입니다. 계좌·통화 확인으로 읽기 문제를 넘기지 않습니다.')
            if masked_identifier(data.identifier):
                raise DomainError('DOCUMENT_CONTEXT_MASKED', '가려진 계좌 번호로는 문서의 계좌를 확정할 수 없습니다.')
            facts = self.db.list(c, company, 'company_facts')
            profile = facts[0] if facts else None
            if data.identifier not in confirmed_bank_identifiers(profile):
                raise DomainError('DOCUMENT_CONTEXT_NOT_CANDIDATE', '확인된 회사 계좌 중에서 이 문서의 계좌를 선택해야 합니다. 계좌를 추정하지 않습니다.')
            records = self._document_records(c, company, document_id)
            if not missing_bank_fields(records):
                raise DomainError('DOCUMENT_CONTEXT_NOT_MISSING', '원본에 계좌와 통화가 있습니다. 문서 확인으로 원본 값을 바꾸지 않습니다.')
            conflicts = context_conflicts(records, data.identifier, data.currency)
            if conflicts:
                raise DomainError('DOCUMENT_CONTEXT_CONFLICT', '원본의 계좌·통화 또는 가려진 식별과 충돌하는 확인은 적용하지 않습니다.', details=conflicts)
            if krw_fraction_fields(records, data.currency):
                raise DomainError('KRW_FRACTION_REVIEW', '원화로 확인한 금액에 소수 단위가 있습니다. 원본은 바꾸지 않으며 이 확인은 적용하지 않습니다.')
            ident = 'docctx_' + digest({'company': company, 'document': document_id})
            existing = c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='document_source_context' AND id=?", (company, ident)).fetchone()
            current = self.db.get(c, company, 'document_source_context', ident) if existing else None
            if data.expected_context_revision != (current['revision'] if current else 0):
                raise DomainError('STALE_VERSION', '문서 확인이 바뀌었습니다. 현재 내용을 다시 확인하세요.')
            body = {'document_id': document_id, 'source_hash': document['sha256'], 'generation': generation, 'document_revision': document['revision'],
                    'account_kind': 'bank', 'identifier': data.identifier, 'currency': data.currency, 'currency_origin': 'owner_statement',
                    'instrument_currency_applied': False, 'confirmed_by': actor.id, 'basis': 'owner_factual_statement', 'active': True,
                    'applied_to_original': False, 'invalidation_reason': None}
            saved = self.db.replace(c, company, 'document_source_context', ident, body, current['revision']) if current else self.db.create(c, company, 'document_source_context', body, ident=ident)
            self.db.audit(c, company, actor.id, 'document.context_confirmed', document_id, {
                'confirmation_id': saved['id'], 'revision': saved['revision'], 'source_hash': document['sha256'], 'generation': generation,
                'currency_origin': 'owner_statement', 'instrument_currency_applied': False, 'applied_to_original': False, 'basis': 'owner_factual_statement'})
            self.db.create(c, company, 'document_context_request', {'request_hash': request_hash, 'confirmation_id': saved['id'], 'actor': actor.id}, ident=key)
            return {**self._context_response(saved), 'reused': False, **self._resume_context(c, company, actor.id)}

    def home(self, actor, company):
        with self.db.connect() as c:
            role = self.auth.guard(c, actor, company)
            profile = json.loads(c.execute('SELECT body FROM companies WHERE id=?', (company,)).fetchone()[0])
            documents = [dict(r) for r in c.execute('SELECT id,filename,state,revision,inspection,result,created_at FROM upload_documents WHERE company_id=? ORDER BY created_at DESC LIMIT 200', (company,))]
            for d in documents:
                d['inspection'] = json.loads(d['inspection'])
                result = json.loads(d.pop('result')) if d['result'] else {}
                d['record_count'] = result.get('record_count', 0)
                d['document_kind'] = result.get('document_kind')
                d['issues'] = result.get('issues', [])[:10]
                d['accounting_complete'] = False
            counts = {r['state']: r['n'] for r in c.execute('SELECT state,count(*) AS n FROM upload_documents WHERE company_id=? GROUP BY state', (company,))}
            pending = sum(count for state, count in counts.items() if state not in ('NORMALIZED', 'QUEUED', 'PROCESSING'))
            journals = self.db.list(c, company, 'journal')
            source_cases = c.execute("SELECT count(*) FROM objects WHERE company_id=? AND kind='ingestion_case' AND json_extract(body,'$.state')!='SUPERSEDED'", (company,)).fetchone()[0]
            legacy_cases = self.db.list(c, company, 'case')
            facts = self.db.list(c, company, 'company_facts')
            current = facts[0] if facts else None
            candidates, _company_candidates = self._source_candidates(c, company)
            confirmed = {(item['kind'], re.sub(r'[ -]', '', item['identifier']).upper()) for item in (current or {}).get('instruments', [])}
            pending_instruments, seen = [], set()
            for item in candidates:
                key = (item['kind'], item['identifier'])
                if key in seen or key in confirmed:
                    continue
                seen.add(key)
                pending_instruments.append(item)
            actions = [{'kind': 'confirm_company', 'label': '회사와 계좌 정보를 확인해 주세요.'}] if not facts else []
            if facts and pending_instruments:
                actions.append({'kind': 'confirm_instruments', 'label': '원본에서 찾은 계좌·카드를 회사 소유인지 확인해 주세요.'})
            from .source_identity import confirmed_bank_identifiers
            if facts and not pending_instruments and not confirmed_bank_identifiers(current):
                actions.append({'kind': 'confirm_owner_account', 'label': '확인된 회사 계좌가 없습니다. 대표가 아는 계좌 번호와 통화만 사실로 남겨 주세요.'})
            context_actions, context_details = self._context_actions(c, company)
            actions.extend(context_actions)
            for d in documents:
                extra = context_details.get(d['id'], {})
                d['reader_ready'] = extra.get('reader_ready', d['state'] == 'NORMALIZED')
                d['missing_source_context'] = extra.get('missing_source_context')
                d['owner_context'] = extra.get('owner_context')
            return {'company': {'id': company, 'name': profile['name'], 'demo': bool(profile.get('demo')), 'role': role},
                    'counts': {'documents': sum(counts.values()), 'read': counts.get('NORMALIZED', 0),
                               'reading': counts.get('QUEUED', 0) + counts.get('PROCESSING', 0), 'needs_attention': pending,
                               'source_cases': source_cases, 'posted_journals': sum(j.get('state') == 'POSTED' for j in journals),
                               'context_attention': len(context_actions),
                               'existing_review_cases': sum(x.get('state') not in ('CLOSED', 'SUPERSEDED') for x in legacy_cases)},
                    'needs_company_confirmation': not bool(facts), 'pending_instrument_candidates': pending_instruments, 'documents': documents,
                    'owner_actions': actions,
                    'initial_ingestion_waiting_means_refresh_skipped': False,
                    'accounting_complete': False, 'live_filing': False,
                    'automation_stage': 'source_reading', 'expert_connected': False,
                    'notice': '자료 읽기는 자동으로 진행합니다. 거래 연결·자동기장·세금 신고의 완료를 뜻하지 않습니다.'}
