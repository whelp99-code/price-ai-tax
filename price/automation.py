"""Owner-consented, bounded dispatch of independently reviewed posting policies.

This is a separate execution service, not the document/model proposal worker.
The owner confirms a processing window and cap, never account/tax decisions.
Policy review/activation, facts, source hashes, roles and period locks remain
mandatory at the exact ledger transaction. No bank/filing API is available here.
"""
from __future__ import annotations
import time
from datetime import date,datetime,timezone
from typing import Annotated
from pydantic import Field,StrictBool,StrictInt,StrictStr,model_validator
from .auth import Actor
from .autopolicies import PolicyEngine
from .casework import Casework
from .db import DomainError,digest,now
from .upload_models import UploadInput


class AutomationConsent(UploadInput):
    policy_ids: Annotated[list[StrictStr],Field(min_length=1,max_length=30)]
    period_start: date
    period_end: date
    expires_at: datetime
    max_per_event: Annotated[StrictInt,Field(gt=0,le=1_000_000_000_000)]
    max_total: Annotated[StrictInt,Field(gt=0,le=10_000_000_000_000)]
    max_events: Annotated[StrictInt,Field(ge=1,le=10000)]
    idempotency_key: Annotated[StrictStr,Field(pattern=r'^[A-Za-z0-9_-]{8,120}$')]
    confirmed: StrictBool

    @model_validator(mode='after')
    def check(self):
        if not self.confirmed:raise ValueError('자동 처리 범위를 확인해야 합니다.')
        if self.period_start>self.period_end or (self.period_end-self.period_start).days>366:raise ValueError('처리할 기간은 1사업연도 이내로 지정하세요.')
        if self.expires_at.tzinfo is None:raise ValueError('만료 시각에는 시간대가 필요합니다.')
        if len(set(self.policy_ids))!=len(self.policy_ids):raise ValueError('처리 정책이 중복되었습니다.')
        if self.max_per_event>self.max_total:raise ValueError('1건 한도가 총 처리 한도를 초과합니다.')
        return self


class AutomationAction(UploadInput):
    expected_revision: Annotated[StrictInt,Field(ge=1)]
    reason: Annotated[StrictStr,Field(min_length=1,max_length=1000)]


class AutomaticBooks:
    def __init__(self,service,*,execution_enabled=False,clock=None):
        self.service,self.db,self.auth=service,service.db,service.auth
        self.clock=clock or time.time
        self.execution_enabled=execution_enabled is True
        self.policies=PolicyEngine(service,posting_enabled=self.execution_enabled)
        self.work=Casework(service)

    def _policy_ref(self,c,company,ident):
        policy=self.db.get(c,company,'auto_policy',ident)
        if policy['state']!='ACTIVE' or not policy.get('reviewed_by') or policy['review_hash']!=digest(policy['spec']):
            raise DomainError('AUTOMATION_POLICY_NOT_READY','다른 검토자의 정답 검증과 활성화가 끝난 정책만 자동 실행할 수 있습니다.')
        self.auth.guard(c,Actor(policy['reviewed_by'],'policy-reviewer'),company,('reviewer',))
        self.auth.guard(c,Actor(policy['activated_by'],'policy-owner'),company,('owner',))
        if policy['reviewed_by'] in (policy['author'],policy['activated_by']):raise DomainError('AUTOMATION_INDEPENDENCE','정책 검토와 작성·활성화가 분리되지 않았습니다.')
        return {'id':ident,'revision':policy['revision'],'spec_hash':digest(policy['spec']),'reviewer':policy['reviewed_by']}

    @staticmethod
    def _seal(grant):
        return digest({k:grant[k] for k in ('spec','owner_id','policy_refs')})

    def consent(self,actor,company,data:AutomationConsent):
        data=AutomationConsent.model_validate(data)
        spec=data.model_dump(mode='json',exclude={'confirmed','idempotency_key'})
        expires=data.expires_at.timestamp()
        if not self.clock()<expires<=self.clock()+31*86400:
            raise DomainError('AUTOMATION_EXPIRY','자동 처리 동의의 유효기간은 현재부터 최대 31일입니다.',422)
        ident='automation_'+digest({'company':company,'request':data.idempotency_key})
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner',))
            request_hash=digest({'actor':actor.id,'spec':spec})
            if c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='automation_grant' AND id=?",(company,ident)).fetchone():
                old=self.db.get(c,company,'automation_grant',ident)
                if old['request_hash']!=request_hash:raise DomainError('AUTOMATION_KEY_CONFLICT','다른 처리 범위에 사용한 요청 번호입니다.')
                return old
            for prior in self.db.list(c,company,'automation_grant'):
                if prior['state']=='ACTIVE' and datetime.fromisoformat(prior['spec']['expires_at']).timestamp()>self.clock():
                    raise DomainError('AUTOMATION_ALREADY_ACTIVE','기존 자동 처리 범위를 먼저 중지하세요. 처리 한도를 중복 적용하지 않습니다.')
            refs=[self._policy_ref(c,company,i) for i in sorted(data.policy_ids)]
            body={'spec':spec,'owner_id':actor.id,'policy_refs':refs,'state':'ACTIVE','request_hash':request_hash,
                  'processed_events':0,'processed_gross':0,'bank_transfers':False,'filing_enabled':False}
            body['consent_hash']=self._seal(body)
            result=self.db.create(c,company,'automation_grant',body,ident=ident)
            self.db.audit(c,company,actor.id,'automation.consented',ident,{'policies':len(refs),'execution_enabled':self.execution_enabled,'max_events':data.max_events,'max_total':data.max_total})
            return result

    def _active(self,c,company,ident):
        grant=self.db.get(c,company,'automation_grant',ident)
        if grant['state']!='ACTIVE' or grant.get('consent_hash')!=self._seal(grant):
            raise DomainError('AUTOMATION_STOPPED','자동 처리 동의가 중지되었거나 내용이 변경되었습니다.')
        if datetime.fromisoformat(grant['spec']['expires_at']).timestamp()<=self.clock():
            raise DomainError('AUTOMATION_EXPIRED','자동 처리 동의의 유효기간이 끝났습니다.')
        self.auth.guard(c,Actor(grant['owner_id'],'automation-owner'),company,('owner',))
        recorded=c.execute("SELECT count(*),COALESCE(sum(json_extract(body,'$.gross')),0) FROM objects WHERE company_id=? AND kind='automation_effect' AND json_extract(body,'$.grant_id')=?",(company,ident)).fetchone()
        if (grant['processed_events'],grant['processed_gross'])!=(recorded[0],recorded[1]):
            raise DomainError('AUTOMATION_USAGE_MISMATCH','자동 처리 사용량과 불변 원장 실행 기록이 다릅니다.')
        for ref in grant['policy_refs']:
            if self._policy_ref(c,company,ref['id'])!=ref:
                raise DomainError('AUTOMATION_POLICY_CHANGED','검토 정책이 변경되었습니다. 새 범위 확인이 필요합니다.')
        return grant

    def stop(self,actor,company,ident,data:AutomationAction):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','reviewer'))
            grant=self.db.get(c,company,'automation_grant',ident)
            if not data.reason.strip():raise DomainError('REASON_REQUIRED','중지 사유가 필요합니다.',422)
            if grant['revision']!=data.expected_revision:raise DomainError('STALE_VERSION','자동 처리 상태가 바뀌었습니다.')
            updated=self.db.replace(c,company,'automation_grant',ident,{**grant,'state':'STOPPED','stopped_by':actor.id,'stop_reason':data.reason},grant['revision'])
            self.db.audit(c,company,actor.id,'automation.stopped',ident,{'posted_preserved':True})
            return updated

    def _prepare(self,c,company,grant,event_id):
        event=self.work._fresh_event(c,company,event_id)
        view=self.work._view(c,company,event)
        if event['kind']!='invoice':raise DomainError('AUTOMATION_OTHER_WORKFLOW','입출금·급여·자산 등의 처리는 해당 회계 흐름에서 수행합니다.')
        if not grant['spec']['period_start']<=event['values']['date'][:10]<=grant['spec']['period_end']:
            raise DomainError('AUTOMATION_PERIOD','자동 처리 대상 기간이 아닙니다.')
        if event['values']['date'][:10]>datetime.fromtimestamp(self.clock(),timezone.utc).date().isoformat():
            raise DomainError('AUTOMATION_FUTURE','미래 거래를 현재 장부로 확정하지 않습니다.')
        if view['questions']:raise DomainError('AUTOMATION_FACTS_PENDING','필요한 업무 사실에 답하면 자동으로 다시 진행합니다.')
        if event['issues'] or any(v=='unknown' for v in view['facts'].values()):
            raise DomainError('AUTOMATION_REVIEW_NEEDED','추측해서 확정하지 않고 전문 검토를 기다립니다.')
        if any(j.get('economic_event_id')==event_id and j['state']=='POSTED' for j in self.db.list(c,company,'journal')):
            raise DomainError('AUTOMATION_ALREADY_POSTED','이 거래는 이미 장부에 반영했습니다.')
        candidates=[];reasons=[]
        for ref in grant['policy_refs']:
            policy=self.db.get(c,company,'auto_policy',ref['id'])
            try:
                proposal=self.policies._proposal(c,company,policy,event_id)
                candidates.append((policy,proposal))
            except DomainError as error:reasons.append(error.code)
        if not candidates:raise DomainError('AUTOMATION_NO_POLICY','현재 거래에 맞는 검토된 정책이 없습니다.',details={'reasons':sorted(set(reasons))})
        if len(candidates)!=1:raise DomainError('AUTOMATION_POLICY_AMBIGUOUS','둘 이상의 정책이 맞아 임의로 선택하지 않습니다.')
        policy,proposal=candidates[0]
        amount=proposal['transaction'].gross
        if amount>grant['spec']['max_per_event']:raise DomainError('AUTOMATION_EVENT_LIMIT','1건 자동 처리 한도를 넘었습니다.')
        if grant['processed_events']>=grant['spec']['max_events'] or grant['processed_gross']+amount>grant['spec']['max_total']:
            raise DomainError('AUTOMATION_TOTAL_LIMIT','합의한 자동 처리 건수 또는 금액 한도에 도달했습니다.')
        return policy,proposal

    def process(self,company,grant_id,event_id):
        """Deployment executor entry point; never attached to the proposal worker."""
        if not self.execution_enabled:raise DomainError('AUTOMATION_EXECUTION_DISABLED','이 배포의 무인 장부 실행은 활성화되지 않았습니다.',403)
        with self.db.transaction() as c:
            grant=self._active(c,company,grant_id)
            effect='auto_effect_'+digest({'company':company,'grant':grant_id,'event':event_id})
            if c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='automation_effect' AND id=?",(company,effect)).fetchone():
                return {**self.db.get(c,company,'automation_effect',effect),'reused':True}
            policy,proposal=self._prepare(c,company,grant,event_id)
            owner=Actor(grant['owner_id'],'consenting-owner')
            authorization=self.policies._evaluate_in(c,owner,company,policy['id'],event_id)
            posted=self.policies._execute_in(c,owner,company,authorization['id'])
            amount=proposal['transaction'].gross
            self.db.replace(c,company,'automation_grant',grant_id,{**grant,'processed_events':grant['processed_events']+1,'processed_gross':grant['processed_gross']+amount},grant['revision'])
            result=self.db.create(c,company,'automation_effect',{'grant_id':grant_id,'event_id':event_id,'journal_id':posted['id'],
                'authorization_id':authorization['id'],'input_hash':proposal['input_hash'],'gross':amount,'state':'POSTED',
                'bookkeeping_complete':True,'filing_complete':False,'bank_transfer_initiated':False,'performed_at':now()},ident=effect)
            self.db.audit(c,company,'system:automatic-books','automation.event_posted',effect,{'journal_id':posted['id'],'grant_id':grant_id,'gross':amount})
            return {**result,'reused':False}

    def run_once(self,company=None):
        if not self.execution_enabled:return {'processed':False,'state':'EXECUTION_DISABLED'}
        with self.db.connect() as c:
            companies=[r[0] for r in c.execute('SELECT id FROM companies'+(' WHERE id=?' if company else '')+' ORDER BY id',([company] if company else []))]
        held=0
        for cid in companies:
            with self.db.connect() as c:
                grants=[g for g in self.db.list(c,cid,'automation_grant') if g['state']=='ACTIVE']
                events=[e for e in self.db.list(c,cid,'economic_event') if e.get('lifecycle')=='CURRENT' and e['kind']=='invoice']
            for grant in grants:
                for event in events:
                    try:
                        result=self.process(cid,grant['id'],event['id'])
                        if not result['reused']:return {'processed':True,'state':'POSTED','journal_count':1,'event_id':event['id']}
                    except DomainError:held+=1
        return {'processed':False,'state':'NO_ELIGIBLE_WORK','held_checks':held}

    def status(self,actor,company):
        with self.db.connect() as c:
            self.auth.guard(c,actor,company)
            grants=self.db.list(c,company,'automation_grant');events=self.db.list(c,company,'economic_event')
            effects=self.db.list(c,company,'automation_effect');held=[]
            for grant in grants:
                if grant['state']!='ACTIVE':continue
                try:self._active(c,company,grant['id'])
                except DomainError as err:
                    held.append({'grant_id':grant['id'],'code':err.code,'message':err.message});continue
                for event in events:
                    if event.get('lifecycle')!='CURRENT' or event['kind']!='invoice':continue
                    try:
                        self._prepare(c,company,grant,event['id'])
                    except DomainError as err:
                        if err.code!='AUTOMATION_ALREADY_POSTED':held.append({'event_id':event['id'],'code':err.code,'message':err.message})
            return {'execution_enabled':self.execution_enabled,'grants':grants,'posted_events':len(effects),
                    'posted_gross':sum(x['gross'] for x in effects),'effects':effects[-100:],'held':held[:200],
                    'held_total':len(held),'whole_company_complete':False,'live_filing':False,
                    'notice':'검토된 정책과 처리 동의 범위의 장부 반영입니다. 신고·납부 완료는 별도로 확인합니다.'}
