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
            if number:
                other = c.execute("SELECT 1 FROM objects WHERE kind='company_facts' AND company_id<>? AND json_extract(body,'$.business_number')=?", (company, number)).fetchone()
                if other:
                    raise DomainError('COMPANY_IDENTITY_CONFLICT', '다른 작업공간과 회사 식별 정보가 충돌합니다. 운영 확인이 필요합니다.')
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
            affected = hold_sources_on_fact_change(self.db, c, company, result, actor.id)
            return {**result, 'source_context_held_ids': affected}

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
            return {'company': {'id': company, 'name': profile['name'], 'demo': bool(profile.get('demo')), 'role': role},
                    'counts': {'documents': sum(counts.values()), 'read': counts.get('NORMALIZED', 0),
                               'reading': counts.get('QUEUED', 0) + counts.get('PROCESSING', 0), 'needs_attention': pending,
                               'source_cases': source_cases, 'posted_journals': sum(j.get('state') == 'POSTED' for j in journals),
                               'existing_review_cases': sum(x.get('state') not in ('CLOSED', 'SUPERSEDED') for x in legacy_cases)},
                    'needs_company_confirmation': not bool(facts), 'pending_instrument_candidates': pending_instruments, 'documents': documents,
                    'owner_actions': actions,
                    'accounting_complete': False, 'live_filing': False,
                    'automation_stage': 'source_reading', 'expert_connected': False,
                    'notice': '자료 읽기는 자동으로 진행합니다. 거래 연결·자동기장·세금 신고의 완료를 뜻하지 않습니다.'}
