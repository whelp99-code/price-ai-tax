"""Reviewed narrow policy evaluations and single-use posting authorizations.

The HTTP application constructs this service with posting disabled. Functional
posting is available to an explicitly configured execution service after release
acceptance; the proposal worker never gets that capability. No policy is seeded.
"""
from __future__ import annotations
import time
from datetime import date
from decimal import Decimal
from typing import Annotated,Literal
from pydantic import Field,StrictBool,StrictInt,StrictStr,model_validator
from .auth import Actor
from .db import DomainError,digest
from .models import TransactionInput,Line
from .upload_models import UploadInput
from .casework import Casework


class PolicyInput(UploadInput):
    name: Annotated[StrictStr,Field(min_length=1,max_length=120)]
    direction: Literal['sale','purchase']
    counterparties: Annotated[list[Annotated[StrictStr,Field(pattern=r'^[0-9]{10}$')]],Field(min_length=1,max_length=30)]
    use_type: Literal['operations','resale']='operations'
    tax_category: Literal['standard','exempt','zero']
    deductible: StrictBool
    amount_limit: Annotated[StrictInt,Field(gt=0,le=1_000_000_000_000)]
    effective_from: date
    effective_to: date
    authority_ids: Annotated[list[StrictStr],Field(min_length=1,max_length=20)]
    @model_validator(mode='after')
    def conditions(self):
        if self.effective_from>self.effective_to:raise ValueError('정책 적용기간을 확인하세요.')
        if self.tax_category!='standard' and self.deductible:raise ValueError('이 정책의 공제 조건과 과세구분이 맞지 않습니다.')
        if len(set(self.counterparties))!=len(self.counterparties) or len(set(self.authority_ids))!=len(self.authority_ids):raise ValueError('정책 조건이 중복되었습니다.')
        return self


class PolicyTruthCase(UploadInput):
    event_id:StrictStr
    expected_lines:Annotated[list[Line],Field(min_length=2,max_length=10)]


class PolicyReview(UploadInput):
    expected_revision:Annotated[StrictInt,Field(ge=1)]
    cases:Annotated[list[PolicyTruthCase],Field(min_length=1,max_length=100)]
    reason:Annotated[StrictStr,Field(min_length=1,max_length=2000)]


class PolicyAction(UploadInput):
    expected_revision:Annotated[StrictInt,Field(ge=1)]
    reason:Annotated[StrictStr,Field(min_length=1,max_length=1000)]


class PolicyEngine:
    def __init__(self,service,*,posting_enabled=False):
        self.service,self.db,self.auth,self.casework=service,service.db,service.auth,Casework(service)
        self.posting_enabled=posting_enabled is True

    def create(self,actor,company,data:PolicyInput):
        data=PolicyInput.model_validate(data)
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','preparer'))
            for authority in data.authority_ids:self.db.get(c,company,'authority',authority)
            policy=self.db.create(c,company,'auto_policy',{'spec':data.model_dump(mode='json'),'state':'DRAFT','author':actor.id,'reviewed_by':None,'review_hash':None})
            self.db.audit(c,company,actor.id,'policy.created',policy['id'])
            return policy

    def _authorities(self,c,company,policy,on_date):
        values=[]
        for ident in policy['spec']['authority_ids']:
            authority=self.db.get(c,company,'authority',ident)
            if not authority.get('reviewed') or not authority.get('reviewed_by') or not authority['effective_from']<=on_date<=authority['effective_to']:
                raise DomainError('POLICY_AUTHORITY','정책 근거의 검토·효력기간을 확인해야 합니다.')
            self.auth.guard(c,Actor(authority['reviewed_by'],'authority-reviewer'),company,('owner','reviewer'))
            values.append((authority['id'],authority['revision'],digest(authority)))
        return values

    def _proposal(self,c,company,policy,event_id):
        event=self.casework._fresh_event(c,company,event_id)
        view=self.casework._view(c,company,event)
        spec=policy['spec'];values=event['values']
        if event['issues'] or event['kind']!='invoice' or event['direction']!=spec['direction'] or event['ownership']!='owner_confirmed':
            raise DomainError('POLICY_SOURCE_SCOPE','확인된 정상 회사 세금계산서 범위가 아닙니다.')
        if event['counterparty_id'] not in spec['counterparties'] or values['currency']!='KRW':raise DomainError('POLICY_PARTY','승인된 거래처와 통화 범위가 아닙니다.')
        on_date=(values['date'] or '')[:10]
        if not spec['effective_from']<=on_date<=spec['effective_to']:raise DomainError('POLICY_PERIOD','정책 적용기간 밖의 거래입니다.')
        self.service._locked(c,company,on_date)
        if event['direction']=='purchase' and (view['facts'].get('business_use')!='business' or view['facts'].get('use_type')!=spec['use_type']):
            raise DomainError('POLICY_FACTS','업무 사용 사실 또는 용도가 정책과 맞지 않습니다.')
        money={}
        for field in ('net','tax','gross'):
            if values.get(field) is None:raise DomainError('POLICY_AMOUNT','원본 금액이 확인되지 않았습니다.')
            decimal=Decimal(values[field])
            if decimal!=decimal.to_integral_value() or decimal<0:raise DomainError('POLICY_AMOUNT','원화 금액과 수정 여부를 확인해야 합니다.')
            money[field]=int(decimal)
        if not 0<money['gross']<=spec['amount_limit'] or money['net']+money['tax']!=money['gross']:raise DomainError('POLICY_AMOUNT','원본 총액과 정책 한도가 맞지 않습니다.')
        if spec['tax_category']!='standard' and money['tax']!=0:raise DomainError('POLICY_TAX_CATEGORY','원본 세액과 검토된 세무 분류가 맞지 않습니다.')
        authorities=self._authorities(c,company,policy,on_date)
        answers=self.casework._answers(c,company,event)
        tx=TransactionInput(external_id=event['id'],source='policy-event',kind='invoice',direction=event['direction'],event_date=on_date,
            counterparty=event['display_counterparty'][:160],currency='KRW',**money,tax_category=spec['tax_category'],deductible=spec['deductible'],
            business_purpose='확인한 회사 업무 사실과 검토된 제한 정책',use_type='resale' if spec['use_type']=='resale' else 'expense',evidence_ids=event['evidence_ids'])
        assessment=self.service._assessment(c,company,tx.model_dump(mode='json'))
        if assessment['questions'] or assessment['blockers']:raise DomainError('POLICY_ASSESSMENT','정책 처리안에 해결되지 않은 조건이 있습니다.')
        fingerprint=digest({'event_hash':event['context_hash'],'facts':[(key,a['id'],a['revision'],a['answer']) for key,a in sorted(answers.items())],
                            'policy_spec':spec,'authorities':authorities,'lines':assessment['lines']})
        return {'event':event,'transaction':tx,'lines':assessment['lines'],'input_hash':fingerprint,'authorities':authorities}

    def review(self,actor,company,ident,data:PolicyReview):
        data=PolicyReview.model_validate(data)
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('reviewer',))
            policy=self.db.get(c,company,'auto_policy',ident)
            if policy['revision']!=data.expected_revision or policy['state']!='DRAFT':raise DomainError('POLICY_STATE','검토할 정책 버전이 바뀌었습니다.')
            if policy['author']==actor.id:raise DomainError('SELF_APPROVAL','정책 작성자와 검토자는 달라야 합니다.',403)
            if len({case.event_id for case in data.cases})!=len(data.cases):raise DomainError('POLICY_TRUTH_DUPLICATE','같은 사례를 반복하여 시험 수를 늘릴 수 없습니다.')
            truth=[]
            for case in data.cases:
                proposal=self._proposal(c,company,policy,case.event_id)
                normalize=lambda lines:sorted((x['account'],x['side'],x['amount']) for x in lines)
                if normalize(proposal['lines'])!=normalize([x.model_dump() for x in case.expected_lines]):raise DomainError('POLICY_TRUTH_MISMATCH','독립 기대 전표와 계산된 전표가 다릅니다.')
                truth.append({'event_id':case.event_id,'input_hash':proposal['input_hash'],'expected_lines':[x.model_dump() for x in case.expected_lines]})
            body={**policy,'state':'REVIEWED','reviewed_by':actor.id,'review_hash':digest(policy['spec']),'truth_cases':truth,'review_reason':data.reason,'review_basis':'application_recorded_independent_cases','legal_professional_signoff':False}
            updated=self.db.replace(c,company,'auto_policy',ident,body,policy['revision'])
            self.db.audit(c,company,actor.id,'policy.reviewed',ident,{'cases':len(truth),'hash':body['review_hash']})
            return updated

    def activate(self,actor,company,ident,data:PolicyAction):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner',))
            p=self.db.get(c,company,'auto_policy',ident)
            if p['revision']!=data.expected_revision or p['state']!='REVIEWED':raise DomainError('POLICY_STATE','독립 사례 검토가 완료된 정책만 활성화할 수 있습니다.')
            self.auth.guard(c,Actor(p['reviewed_by'],'policy-reviewer'),company,('reviewer',))
            if actor.id==p['reviewed_by'] or p['review_hash']!=digest(p['spec']):raise DomainError('POLICY_INDEPENDENCE','정책 검토와 배포 권한을 분리해야 합니다.',403)
            updated=self.db.replace(c,company,'auto_policy',ident,{**p,'state':'ACTIVE','activated_by':actor.id},p['revision'])
            self.db.audit(c,company,actor.id,'policy.activated',ident,{'execution_enabled':self.posting_enabled,'reason':data.reason})
            return updated

    def evaluate(self,actor,company,ident,event_id):
        with self.db.transaction() as c:
            return self._evaluate_in(c,actor,company,ident,event_id)

    def _evaluate_in(self,c,actor,company,ident,event_id):
        """The same checks inside a caller-owned atomic bookkeeping transaction."""
        self.auth.guard(c,actor,company,('owner','preparer','reviewer'))
        p=self.db.get(c,company,'auto_policy',ident)
        proposal=self._proposal(c,company,p,event_id)
        if p['state']!='ACTIVE' or p['review_hash']!=digest(p['spec']):raise DomainError('POLICY_INACTIVE','활성화된 검토 정책이 아닙니다.')
        self.auth.guard(c,Actor(p['reviewed_by'],'policy-reviewer'),company,('reviewer',))
        auth_id='posting_auth_'+digest({'policy':ident,'revision':p['revision'],'event':event_id,'input':proposal['input_hash']})
        old=c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='posting_authorization' AND id=?",(company,auth_id)).fetchone()
        if old:return self.db.get(c,company,'posting_authorization',auth_id)
        auth=self.db.create(c,company,'posting_authorization',{'kind':'APPROVED_POLICY','policy_id':ident,'policy_revision':p['revision'],'event_id':event_id,
            'input_hash':proposal['input_hash'],'lines':proposal['lines'],'expires_at':int(time.time())+3600,'state':'READY','journal_id':None},ident=auth_id)
        self.db.audit(c,company,actor.id,'policy.evaluated',auth_id,{'event_id':event_id,'execution_enabled':self.posting_enabled})
        return auth

    def execute(self,actor,company,ident):
        if not self.posting_enabled:raise DomainError('POLICY_EXECUTION_NOT_RELEASED','운영 정책기장은 실제 자료·전문 검토 인수 전이며 아직 활성화되지 않았습니다.',403)
        with self.db.transaction() as c:
            return self._execute_in(c,actor,company,ident)

    def _execute_in(self,c,actor,company,ident):
        """Consume the same authorization with all guards inside one transaction."""
        if not self.posting_enabled:raise DomainError('POLICY_EXECUTION_NOT_RELEASED','운영 정책기장은 아직 활성화되지 않았습니다.',403)
        self.auth.guard(c,actor,company,('owner','preparer'))
        a=self.db.get(c,company,'posting_authorization',ident)
        if a['state']=='EXECUTED':return self.db.get(c,company,'journal',a['journal_id'])
        p=self.db.get(c,company,'auto_policy',a['policy_id'])
        if a['state']!='READY' or a['expires_at']<=time.time() or p['state']!='ACTIVE' or p['revision']!=a['policy_revision'] or p['review_hash']!=digest(p['spec']):raise DomainError('POLICY_AUTH_INVALID','정책 실행 허가가 변경·만료·철회되었습니다.')
        self.auth.guard(c,Actor(p['reviewed_by'],'policy-reviewer'),company,('reviewer',))
        self.auth.guard(c,Actor(p['activated_by'],'policy-activator'),company,('owner',))
        proposal=self._proposal(c,company,p,a['event_id'])
        if proposal['input_hash']!=a['input_hash'] or proposal['lines']!=a['lines']:raise DomainError('POLICY_INPUT_CHANGED','원본·사실·근거 또는 처리안이 달라졌습니다.')
        if any(j.get('economic_event_id')==a['event_id'] and j['state']=='POSTED' for j in self.db.list(c,company,'journal')):raise DomainError('DUPLICATE_POST','이 경제적 거래는 이미 장부에 반영되었습니다.')
        event=proposal['event'];system=Actor('system:policy-executor','policy-executor')
        tx=self.service._new_tx(c,system,company,proposal['transaction'])
        j=self.service._new_journal(c,system,company,{'event_date':tx['event_date'],'description':'검토된 정책: '+p['spec']['name'],'lines':proposal['lines'],'evidence_ids':event['evidence_ids']},transaction=tx)
        self.service._journal_valid(c,company,j)
        posted=self.db.replace(c,company,'journal',j['id'],{**j,'state':'POSTED','economic_event_id':event['id'],'authorization_kind':'APPROVED_POLICY','authorization_id':ident,'input_hash':a['input_hash'],'policy_id':p['id'],'approved_by':None},j['revision'])
        self.db.replace(c,company,'posting_authorization',ident,{**a,'state':'EXECUTED','journal_id':posted['id']},a['revision'])
        self.db.audit(c,company,system.id,'journal.policy_posted',posted['id'],{'authorization_id':ident,'policy_id':p['id'],'input_hash':a['input_hash'],'requested_by':actor.id})
        return posted

    def revoke(self,actor,company,ident,data:PolicyAction):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','reviewer'))
            p=self.db.get(c,company,'auto_policy',ident)
            if p['revision']!=data.expected_revision:raise DomainError('STALE_VERSION','정책이 변경되었습니다.')
            updated=self.db.replace(c,company,'auto_policy',ident,{**p,'state':'REVOKED','revoked_by':actor.id,'reason':data.reason},p['revision'])
            posted=[j['id'] for j in self.db.list(c,company,'journal') if j.get('policy_id')==ident and j['state']=='POSTED']
            self.db.audit(c,company,actor.id,'policy.revoked',ident,{'posted_review_ids':posted})
            return {'policy':updated,'posted_review_ids':posted,'posted_records_changed':False}
