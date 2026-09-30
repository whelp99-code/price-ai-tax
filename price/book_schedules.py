"""Source-backed prepaid/asset subledgers with real, once-only journal posting.

A reviewer approves an exact finite accounting schedule, not arbitrary future
LLM actions. Each posting rechecks source, reviewer, period and schedule hash.
Tax depreciation/deductibility is not inferred by this book-accounting module.
"""
from __future__ import annotations
import calendar
from datetime import date
from typing import Annotated,Literal
from pydantic import Field,StrictInt,StrictStr,model_validator
from .auth import Actor
from .db import DomainError,digest,now
from .upload_models import UploadInput
from .finance_ops import prorate_days,asset_schedule
from .calculations import validate_lines


class BookScheduleInput(UploadInput):
    kind:Literal['prepaid','asset']
    source_journal_id:Annotated[StrictStr,Field(min_length=1,max_length=160)]
    label:Annotated[StrictStr,Field(min_length=1,max_length=200)]
    classification_date:date
    start:date
    end:date|None=None
    months:Annotated[StrictInt,Field(ge=1,le=600)]|None=None
    salvage:Annotated[StrictInt,Field(ge=0,le=1_000_000_000_000)]=0
    evidence_ids:Annotated[list[StrictStr],Field(min_length=1,max_length=30)]
    idempotency_key:Annotated[StrictStr,Field(pattern=r'^[A-Za-z0-9_-]{8,120}$')]

    @model_validator(mode='after')
    def validate_term(self):
        if self.classification_date>self.start:raise ValueError('원가 분류일이 비용 인식 시작보다 늦습니다.')
        if len(set(self.evidence_ids))!=len(self.evidence_ids):raise ValueError('증빙이 중복되었습니다.')
        if self.kind=='prepaid' and (self.end is None or self.end<self.start or (self.end-self.start).days>3660 or self.months is not None or self.salvage):
            raise ValueError('선급비용에는 10년 이내의 유효한 시작·종료일이 필요합니다.')
        if self.kind=='asset' and (self.months is None or self.end is not None):raise ValueError('자산에는 사용기간(개월)과 잔존금액이 필요합니다.')
        return self


class ScheduleReview(UploadInput):
    expected_revision:Annotated[StrictInt,Field(ge=1)]
    reason:Annotated[StrictStr,Field(min_length=1,max_length=2000)]
    @model_validator(mode='after')
    def reason_required(self):
        if not self.reason.strip():raise ValueError('검토·중단 사유가 필요합니다.')
        return self


def _entry(key,when,amount,debit,credit,role):
    return {'key':key,'event_date':when,'amount':amount,'role':role,
            'lines':[{'account':debit,'side':'debit','amount':amount},{'account':credit,'side':'credit','amount':amount}]}


def schedule_hash(plan):
    return digest({k:plan[k] for k in ('input','cost','source_account','source_hash','source_evidence','entries','method','author')})


class BookSchedules:
    def __init__(self,service,*,today=None):
        self.service,self.db,self.auth=service,service.db,service.auth
        self.today=today or date.today

    def _source(self,c,company,ident):
        source=self.db.get(c,company,'journal',ident)
        if source['state']!='POSTED' or source.get('reversal_of'):
            raise DomainError('SCHEDULE_SOURCE_REQUIRED','확정된 원거래 전표가 필요합니다.')
        if any(j.get('reversal_of')==ident and j.get('state')=='POSTED' for j in self.db.list(c,company,'journal')):
            raise DomainError('SCHEDULE_SOURCE_REVERSED','원거래가 정정·취소되어 남은 일정을 검토해야 합니다.')
        if any(ident in x.get('posted_journal_ids',[]) and x.get('state')=='REVIEW_REQUIRED' for x in self.db.list(c,company,'source_change_impact')):
            raise DomainError('SCHEDULE_SOURCE_REVIEW','원본 재처리의 장부 영향 검토를 먼저 해결해야 합니다.')
        self.service._evidence_check(c,company,source['evidence_ids'],required=True)
        return source

    def propose(self,actor,company,data:BookScheduleInput):
        data=BookScheduleInput.model_validate(data)
        values=data.model_dump(mode='json');request_hash=digest(values)
        ident='bookschedule_'+digest({'company':company,'key':data.idempotency_key})
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','preparer','reviewer'))
            if c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='book_schedule' AND id=?",(company,ident)).fetchone():
                old=self.db.get(c,company,'book_schedule',ident)
                if old['request_hash']!=request_hash:raise DomainError('SCHEDULE_KEY_CONFLICT','같은 요청 번호에 다른 일정이 있습니다.')
                return self._view(c,company,old)
            source=self._source(c,company,data.source_journal_id)
            self.service._evidence_check(c,company,data.evidence_ids,required=True)
            if source['event_date']>data.classification_date.isoformat():raise DomainError('SCHEDULE_SOURCE_DATE','원거래보다 이른 날짜에 비용을 재분류하지 않습니다.')
            allowed=('expense','prepaid_expense') if data.kind=='prepaid' else ('expense','fixed_asset')
            balances={account:sum(l['amount']*(1 if l['side']=='debit' else -1) for l in source['lines'] if l['account']==account) for account in allowed}
            positive=[account for account,value in balances.items() if value>0]
            if len(positive)!=1:raise DomainError('SCHEDULE_SOURCE_BASIS','원가를 확인할 수 있는 하나의 확정 비용·자산 금액이 필요합니다.')
            account=positive[0];cost=balances[account]
            if data.salvage>cost:raise DomainError('SCHEDULE_SALVAGE','잔존금액이 취득원가보다 큽니다.')
            if any(p['input']['source_journal_id']==source['id'] and (p['state']!='CANCELLED' or self._posted(c,company,p['id'])) for p in self.db.list(c,company,'book_schedule')):
                raise DomainError('SOURCE_ALREADY_SCHEDULED','같은 원가를 중복으로 배분할 수 없습니다. 기존 일정을 확인하세요.')
            holding='prepaid_expense' if data.kind=='prepaid' else 'fixed_asset'
            entries=[]
            if account!=holding:entries.append(_entry('reclass',data.classification_date.isoformat(),cost,holding,account,'RECLASSIFICATION'))
            if data.kind=='prepaid':
                calculated=prorate_days(cost,data.start,data.end)
                for part in calculated['periods']:
                    if part['amount']:entries.append(_entry('period-'+part['month'],part['end'],part['amount'],'expense','prepaid_expense','PERIOD_EXPENSE'))
            else:
                calculated=asset_schedule(cost,data.salvage,data.months,data.start)
                for part in calculated['periods']:
                    year,month=map(int,part['month'].split('-'));last=date(year,month,calendar.monthrange(year,month)[1]).isoformat()
                    if part['expense']:entries.append(_entry('period-'+part['month'],last,part['expense'],'depreciation_expense','accumulated_depreciation','DEPRECIATION'))
            for entry in entries:validate_lines(entry['lines'])
            if len(entries)>601:raise DomainError('SCHEDULE_LIMIT','일정 개수가 지원 범위를 넘었습니다.')
            source_evidence=sorted(set(source['evidence_ids']+data.evidence_ids))
            plan={'input':values,'cost':cost,'source_account':account,'source_hash':digest(source),'source_evidence':source_evidence,
                  'entries':entries,'method':calculated['method'],'author':actor.id,'state':'DRAFT','approved_by':None,
                  'approval_hash':None,'tax_depreciation_verified':False,'tax_deductibility_decided':False}
            plan.update(request_hash=request_hash)
            result=self.db.create(c,company,'book_schedule',plan,ident=ident)
            self.db.audit(c,company,actor.id,'book_schedule.proposed',ident,{'kind':data.kind,'entries':len(entries),'cost':cost})
            return self._view(c,company,result)

    def _posted(self,c,company,ident):
        return [r for r in self.db.list(c,company,'journal') if r.get('book_schedule_id')==ident and r.get('state')=='POSTED']

    def _view(self,c,company,plan):
        journals=self._posted(c,company,plan['id'])
        reversed_ids={j.get('reversal_of') for j in self.db.list(c,company,'journal') if j.get('state')=='POSTED' and j.get('reversal_of')}
        recognized=sum(j['book_schedule_amount'] for j in journals if j['book_schedule_role']!='RECLASSIFICATION' and j['id'] not in reversed_ids)
        keys={j['book_schedule_key'] for j in journals}
        return {**plan,'posted_journal_ids':[j['id'] for j in journals],'recognized_expense':recognized,
                'book_value':plan['cost']-recognized,'remaining_entries':sum(e['key'] not in keys for e in plan['entries']),
                'reversed_journal_ids':sorted(j['id'] for j in journals if j['id'] in reversed_ids),
                'notice':'검토된 장부상 비용 일정입니다. 법정 세무상각·공제 여부를 자동 결정하지 않습니다.'}

    def get(self,actor,company,ident):
        with self.db.connect() as c:
            self.auth.guard(c,actor,company)
            return self._view(c,company,self.db.get(c,company,'book_schedule',ident))

    def list(self,actor,company):
        with self.db.connect() as c:
            self.auth.guard(c,actor,company)
            return [self._view(c,company,p) for p in self.db.list(c,company,'book_schedule')]

    def approve(self,actor,company,ident,data:ScheduleReview):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','reviewer'))
            plan=self.db.get(c,company,'book_schedule',ident)
            if plan['author']==actor.id:raise DomainError('SELF_APPROVAL','일정 작성자와 다른 검토자가 원본·비용 인식 방법을 확인해야 합니다.',403)
            self.auth.guard(c,actor,company,('reviewer',))
            if plan['revision']!=data.expected_revision:raise DomainError('STALE_VERSION','일정이 변경되었습니다.')
            if plan['state']!='DRAFT':raise DomainError('SCHEDULE_STATE','초안 일정만 승인할 수 있습니다.')
            source=self._source(c,company,plan['input']['source_journal_id'])
            if digest(source)!=plan['source_hash']:raise DomainError('SCHEDULE_SOURCE_CHANGED','원거래가 변경되었습니다.')
            plan.update(state='APPROVED',approved_by=actor.id,approval_hash=schedule_hash(plan),review_note=data.reason,approved_at=now())
            result=self.db.replace(c,company,'book_schedule',ident,plan,data.expected_revision)
            self.db.audit(c,company,actor.id,'book_schedule.approved',ident,{'schedule_hash':result['approval_hash'],'reason':data.reason})
            return self._view(c,company,result)

    def _validate(self,c,company,plan):
        if plan['state'] not in ('APPROVED','COMPLETE'):raise DomainError('SCHEDULE_NOT_APPROVED','독립 검토된 일정만 장부에 반영합니다.')
        if not plan['approved_by'] or plan['approved_by']==plan['author'] or plan['approval_hash']!=schedule_hash(plan):
            raise DomainError('SCHEDULE_APPROVAL_CHANGED','검토한 일정의 내용이 바뀌었습니다.')
        self.auth.guard(c,Actor(plan['approved_by'],'schedule-reviewer'),company,('reviewer',))
        posted_ids={j['id'] for j in self._posted(c,company,plan['id'])}
        if any(j.get('reversal_of') in posted_ids and j.get('state')=='POSTED' for j in self.db.list(c,company,'journal')):
            raise DomainError('SCHEDULE_ENTRY_REVERSED','이미 반영한 일정이 정정되어 남은 일정을 재검토해야 합니다.')
        source=self._source(c,company,plan['input']['source_journal_id'])
        if digest(source)!=plan['source_hash']:raise DomainError('SCHEDULE_SOURCE_CHANGED','원거래가 변경되었습니다.')
        self.service._evidence_check(c,company,plan['source_evidence'],required=True)
        return source

    def _post(self,c,company,plan,through,actor):
        self._validate(c,company,plan)
        previous=self._posted(c,company,plan['id']);keys={j['book_schedule_key'] for j in previous}
        due=[entry for entry in plan['entries'] if entry['event_date']<=through.isoformat() and entry['key'] not in keys]
        for row in due:self.service._locked(c,company,row['event_date'])
        created=[]
        for row in due:
            data={'event_date':row['event_date'],'description':plan['input']['label']+' · '+row['role'],
                  'lines':row['lines'],'evidence_ids':plan['source_evidence']}
            journal=self.service._new_journal(c,Actor('system:book-schedules','book-schedules'),company,data)
            self.service._journal_valid(c,company,journal)
            journal.update(state='POSTED',approved_by=plan['approved_by'],approval_hash=self.service.journal_hash(journal),
                           authorization_kind='REVIEWED_FINITE_SCHEDULE',book_schedule_id=plan['id'],book_schedule_key=row['key'],
                           book_schedule_amount=row['amount'],book_schedule_role=row['role'],book_schedule_hash=plan['approval_hash'])
            journal=self.db.replace(c,company,'journal',journal['id'],journal,journal['revision'])
            created.append(journal['id']);keys.add(row['key'])
            self.db.audit(c,company,actor,'book_schedule.posted',journal['id'],{'plan':plan['id'],'entry':row['key'],'source':plan['input']['source_journal_id'],'schedule_hash':plan['approval_hash']})
        if len(keys)==len(plan['entries']) and plan['state']!='COMPLETE':
            plan=self.db.replace(c,company,'book_schedule',plan['id'],{**plan,'state':'COMPLETE','completed_at':now()},plan['revision'])
        return {**self._view(c,company,plan),'created_journal_ids':created}

    def post_due(self,actor,company,ident,through):
        if type(through) is not date or through>self.today():raise DomainError('SCHEDULE_FUTURE_POST','미래 일자의 장부를 미리 확정하지 않습니다.',422)
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','preparer','reviewer'))
            return self._post(c,company,self.db.get(c,company,'book_schedule',ident),through,actor.id)

    def cancel(self,actor,company,ident,data:ScheduleReview):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','reviewer'))
            plan=self.db.get(c,company,'book_schedule',ident)
            if plan['revision']!=data.expected_revision:raise DomainError('STALE_VERSION','일정이 변경되었습니다.')
            if plan['state'] not in ('DRAFT','APPROVED'):raise DomainError('SCHEDULE_STATE','현재 일정은 중단할 수 없습니다.')
            plan=self.db.replace(c,company,'book_schedule',ident,{**plan,'state':'CANCELLED','cancel_reason':data.reason},data.expected_revision)
            self.db.audit(c,company,actor.id,'book_schedule.cancelled',ident,{'reason':data.reason,'posted_preserved':True})
            return self._view(c,company,plan)

    def run_once(self,company=None):
        with self.db.transaction() as c:
            new_errors=0
            companies=[r[0] for r in c.execute('SELECT id FROM companies'+(' WHERE id=?' if company else ''),[company] if company else [])]
            for company_id in companies:
                for plan in self.db.list(c,company_id,'book_schedule'):
                    if plan['state']!='APPROVED':continue
                    keys={j['book_schedule_key'] for j in self._posted(c,company_id,plan['id'])}
                    if not any(e['key'] not in keys and e['event_date']<=self.today().isoformat() for e in plan['entries']):continue
                    c.execute('SAVEPOINT finite_schedule')
                    try:
                        result=self._post(c,company_id,plan,self.today(),'system:book-schedules')
                        c.execute('RELEASE finite_schedule')
                        return {'processed':True,'state':'POSTED','journal_count':len(result['created_journal_ids'])}
                    except DomainError as error:
                        c.execute('ROLLBACK TO finite_schedule');c.execute('RELEASE finite_schedule')
                        ident='schedule_block_'+digest({'plan':plan['id'],'approval':plan['approval_hash'],'code':error.code})
                        if not c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='schedule_exception' AND id=?",(company_id,ident)).fetchone():
                            self.db.create(c,company_id,'schedule_exception',{'plan_id':plan['id'],'state':'OPEN','code':error.code},ident=ident)
                            self.db.audit(c,company_id,'system:book-schedules','book_schedule.blocked',plan['id'],{'code':error.code})
                            new_errors+=1
            if new_errors:return {'processed':True,'state':'REVIEW_REQUIRED','blocked_schedules':new_errors}
        return {'processed':False}


def due_schedule_blockers(db,c,company,period):
    journals=db.list(c,company,'journal');blockers=[]
    for plan in db.list(c,company,'book_schedule'):
        if plan['input']['classification_date']>period['end']:continue
        posted={j['book_schedule_key']:j['id'] for j in journals if j.get('book_schedule_id')==plan['id'] and j['state']=='POSTED'}
        missing=[e['key'] for e in plan['entries'] if e['event_date']<=period['end'] and e['key'] not in posted]
        reversed_entries=[j for j in journals if j.get('reversal_of') in posted.values() and j['state']=='POSTED' and j['event_date']<=period['end']]
        if missing:blockers.append({'code':'SCHEDULE_NOT_POSTED','id':plan['id'],'message':'검토·반영이 필요한 자산 또는 기간 비용이 있습니다.','pending_entries':len(missing)})
        if reversed_entries:blockers.append({'code':'SCHEDULE_REVERSED_REVIEW','id':plan['id'],'message':'정정된 비용 일정의 남은 금액을 확인해야 합니다.'})
    return blockers
