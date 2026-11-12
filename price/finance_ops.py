"""Period-level source completeness and deterministic accounting schedules.

Schedules are proposals, not automatic statutory depreciation or tax rules.
Cash movements, accrual profit and unavailable opening balances stay distinct.
"""
from __future__ import annotations
import calendar
import json
from collections import defaultdict
from datetime import date,timedelta
from decimal import Decimal
from typing import Annotated,Literal,TypedDict
from pydantic import Field,StrictInt,StrictStr,model_validator
from .db import DomainError,digest
from .upload_models import UploadInput
from .accounts import EXPENSE_ACCOUNTS,INCOME_ACCOUNTS


def bounded_money(value):
    if type(value) is not int or not 0<=value<=1_000_000_000_000:
        raise ValueError('Money must be a bounded nonnegative integer KRW amount')
    return value


def prorate_days(total:int,start:date,end:date):
    bounded_money(total)
    if type(start) is not date or type(end) is not date or start>end or (end-start).days>36600:raise ValueError('Invalid accrual date range')
    count=(end-start).days+1;cursor=start;used=0;parts=[]
    while cursor<=end:
        last=min(date(cursor.year,cursor.month,calendar.monthrange(cursor.year,cursor.month)[1]),end)
        cumulative=(last-start).days+1
        allocated=total*cumulative//count-used
        parts.append({'month':cursor.strftime('%Y-%m'),'start':cursor.isoformat(),'end':last.isoformat(),'days':(last-cursor).days+1,'amount':allocated})
        used+=allocated;cursor=last+timedelta(days=1)
    return {'method':'cumulative_day_fraction_integer_floor','total':total,'days':count,'periods':parts,'conserved':sum(x['amount'] for x in parts)==total}


def asset_schedule(cost:int,salvage:int,months:int,start:date):
    bounded_money(cost);bounded_money(salvage)
    if salvage>cost or type(months) is not int or not 1<=months<=1200 or type(start) is not date:raise ValueError('Invalid depreciation proposal')
    amount=cost-salvage;parts=[];previous=0
    for index in range(months):
        absolute=start.year*12+start.month-1+index;year,month=divmod(absolute,12)
        if year>9999:raise ValueError('Schedule exceeds supported calendar')
        cumulative=amount*(index+1)//months
        parts.append({'month':f'{year:04d}-{month+1:02d}','expense':cumulative-previous,'closing_book_value':cost-cumulative})
        previous=cumulative
    return {'method':'book_straight_line_full_month_proposal','cost':cost,'salvage':salvage,'periods':parts,'tax_depreciation_verified':False}


class InventoryLot(TypedDict):
    id: str
    quantity: int
    total_cost: int


def fifo_cost(lots:list[InventoryLot],quantity:int):
    if type(quantity) is not int or quantity<=0:raise ValueError('Positive integer quantity required')
    if not isinstance(lots,list) or len(lots)>10000:raise ValueError('Inventory lot limit')
    total_qty=0;ids=set()
    for lot in lots:
        if not isinstance(lot,dict) or set(lot)!={'id','quantity','total_cost'}:raise ValueError('Explicit lot identity quantity and cost required')
        if not isinstance(lot['id'],str) or not lot['id'] or lot['id'] in ids:raise ValueError('Duplicate or invalid inventory lot identity')
        ids.add(lot['id'])
        if type(lot['quantity']) is not int or lot['quantity']<=0:raise ValueError('Invalid lot quantity')
        bounded_money(lot['total_cost']);total_qty+=lot['quantity']
    if quantity>total_qty:raise ValueError('Cannot consume inventory not present')
    remaining=quantity;used=[];after=[];expense=0
    for lot in lots:
        take=min(remaining,lot['quantity'])
        cost=lot['total_cost']*take//lot['quantity'] if take<lot['quantity'] else lot['total_cost']
        if take:used.append({'lot_id':lot['id'],'quantity':take,'cost':cost});expense+=cost
        if take<lot['quantity']:after.append({**lot,'quantity':lot['quantity']-take,'total_cost':lot['total_cost']-cost})
        remaining-=take
    return {'quantity':quantity,'cost_of_sales':expense,'consumed':used,'remaining_lots':after,'cost_conserved':expense+sum(x['total_cost'] for x in after)==sum(x['total_cost'] for x in lots)}


def uploaded_source_blockers(db,c,company,period):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='upload_documents'").fetchone():return []
    from .journal_lineage import reversal_blockers
    blockers=reversal_blockers(db,c,company,period);start,end=period['start'],period['end']
    events=db.list(c,company,'economic_event')
    posted={j.get('economic_event_id'):j for j in db.list(c,company,'journal') if j.get('state')=='POSTED' and start<=j['event_date']<=end}
    by_source=defaultdict(list)
    for event in events:
        if event.get('lifecycle')=='CURRENT':
            for source in event['source_record_ids']:by_source[source].append(event)
    for doc in c.execute('SELECT * FROM upload_documents WHERE company_id=?',(company,)):
        summary=json.loads(doc['result']) if doc['result'] else {}
        if summary.get('document_kind')=='company_registration':continue
        if summary.get('document_kind') in ('opening_balances','opening_balance'):
            opening=next((p for p in db.list(c,company,'opening_batch') if p['document_id']==doc['id'] and p['state']=='POSTED'),None)
            if opening is None or doc['state']!='NORMALIZED':blockers.append({'code':'OPENING_UNPOSTED','id':doc['id'],'message':'읽은 기초잔액의 독립 검토와 장부 반영이 필요합니다.'})
            continue
        records=[db.unpack(row) for row in c.execute("SELECT * FROM objects WHERE company_id=? AND kind='source_record' AND json_extract(body,'$.document_id')=? AND COALESCE(json_extract(body,'$.lifecycle'),'CURRENT')='CURRENT'",(company,doc['id']))]
        relevant=[r for r in records if not r['values'].get('date') or start<=str(r['values']['date'])[:10]<=end]
        if doc['state']!='NORMALIZED':
            # A period cannot silently discard an unread document with an unknown date.
            if not records or relevant: blockers.append({'code':'UNRESOLVED_SOURCE_DOCUMENT','id':doc['id'],'message':'자료의 기간과 금액 확인이 끝나지 않았습니다: '+doc['filename']})
            continue
        for record in relevant:
            linked=by_source.get(record['id'],[])
            if not linked: blockers.append({'code':'UNLINKED_SOURCE_RECORD','id':record['id'],'message':'읽은 원본이 아직 거래와 장부에 연결되지 않았습니다.'});continue
            from .card_books import is_accounted_card_correction
            if not any(e['id'] in posted and (not e['issues'] or is_accounted_card_correction(db,c,company,e,posted[e['id']])) for e in linked):
                blockers.append({'code':'UNACCOUNTED_SOURCE_EVENT','id':record['id'],'message':'업로드한 거래의 회계 검증과 장부 반영이 필요합니다.'})
    for impact in db.list(c,company,'source_change_impact'):
        if impact.get('state')=='REVIEW_REQUIRED':blockers.append({'code':'SOURCE_CHANGE_REVIEW','id':impact['id'],'message':'다시 읽은 자료가 기존 장부·신고에 미치는 영향을 확인해야 합니다.'})
    return blockers


def source_snapshot(db,c,company,period):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='upload_documents'").fetchone():return None
    documents=[]
    for row in c.execute('SELECT id,sha256,state,revision,result FROM upload_documents WHERE company_id=? ORDER BY id',(company,)):
        summary=json.loads(row['result']) if row['result'] else {}
        if summary.get('document_kind')=='company_registration':continue
        source_rows=c.execute("SELECT id,revision,body FROM objects WHERE company_id=? AND kind='source_record' AND json_extract(body,'$.document_id')=? AND COALESCE(json_extract(body,'$.lifecycle'),'CURRENT')='CURRENT' ORDER BY id",(company,row['id'])).fetchall()
        dates=[str(json.loads(x['body']).get('values',{}).get('date') or '')[:10] for x in source_rows]
        if not dates or any(not d or period['start']<=d<=period['end'] for d in dates):
            documents.append({'id':row['id'],'sha256':row['sha256'],'state':row['state'],'revision':row['revision'],'records':[(x['id'],x['revision'],digest(json.loads(x['body']))) for x in source_rows]})
    return {'documents':documents,'company_facts':db.list(c,company,'company_facts'),'event_facts':db.list(c,company,'event_fact')}


class AccountTotal(TypedDict):
    debit: int
    credit: int
    journal_ids: list[str]

class OwnerReportQuery(UploadInput):
    start: date
    end: date
    expected_source_revision: Annotated[StrictStr,Field(pattern=r'^sha256:[a-f0-9]{64}$',max_length=71)]|None=None


class ScheduleInput(UploadInput):
    kind:Literal['accrual','asset']
    label:Annotated[StrictStr,Field(min_length=1,max_length=120)]
    cost:Annotated[StrictInt,Field(ge=0,le=1_000_000_000_000)]
    start:date
    end:date|None=None
    salvage:Annotated[StrictInt,Field(ge=0,le=1_000_000_000_000)]=0
    months:Annotated[StrictInt,Field(ge=1,le=1200)]|None=None
    evidence_ids:Annotated[list[StrictStr],Field(min_length=1,max_length=30)]
    idempotency_key:Annotated[StrictStr,Field(pattern=r'^[A-Za-z0-9_-]{8,120}$')]
    @model_validator(mode='after')
    def contract(self):
        if self.kind=='accrual' and (not self.end or self.end<self.start or self.salvage or self.months is not None):raise ValueError('계약기간 배분 입력이 맞지 않습니다.')
        if self.kind=='asset' and (not self.months or self.end is not None or self.salvage>self.cost):raise ValueError('자산 배분 입력이 맞지 않습니다.')
        return self


class FinanceOps:
    def __init__(self,service):self.service,self.db,self.auth=service,service.db,service.auth

    def propose_schedule(self,actor,company,data:ScheduleInput):
        data=ScheduleInput.model_validate(data)
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('reviewer','preparer'))
            self.service._evidence_check(c,company,data.evidence_ids,required=True)
            key='schedule_'+digest({'company':company,'key':data.idempotency_key});body=data.model_dump(mode='json');request_hash=digest(body)
            previous=c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='accounting_schedule' AND id=?",(company,key)).fetchone()
            if previous:
                old=self.db.get(c,company,'accounting_schedule',key)
                if old['request_hash']!=request_hash:raise DomainError('SCHEDULE_KEY_CONFLICT','같은 일정 번호에 다른 조건이 있습니다.')
                return old
            if data.kind=='accrual':
                assert data.end is not None
                calculated=prorate_days(data.cost,data.start,data.end)
            else:
                assert data.months is not None
                calculated=asset_schedule(data.cost,data.salvage,data.months,data.start)
            schedule=self.db.create(c,company,'accounting_schedule',{'input':body,'request_hash':request_hash,'result':calculated,'author':actor.id,'state':'PROPOSED','posting_performed':False,'tax_rule_verified':False},ident=key)
            self.db.audit(c,company,actor.id,'schedule.proposed',key,{'kind':data.kind,'cost':data.cost})
            return schedule

    def report(self,actor,company,start:date,end:date,expected_source_revision:str|None=None):
        if start>end:raise DomainError('PERIOD_RANGE','보고 기간이 맞지 않습니다.',422)
        with self.db.connect() as c:
            c.execute('BEGIN')
            role=self.auth.guard(c,actor,company)
            all_journals=[j for j in self.db.list(c,company,'journal') if j['state']=='POSTED' and j['event_date']<=end.isoformat()]
            journals=[j for j in all_journals if start.isoformat()<=j['event_date'] and j.get('current_period_activity') is not False]
            opening=next((j for j in all_journals if j.get('opening_batch_id') and j['event_date']<=start.isoformat()),None)
            if opening and any(j.get('reversal_of')==opening['id'] for j in all_journals):opening=None
            cash_amount=lambda entries:sum(l['amount']*(1 if l['side']=='debit' else -1) for j in entries for l in j['lines'] if l['account']=='cash')
            opening_cash=cash_amount([j for j in all_journals if j['event_date']<start.isoformat() or j.get('opening_batch_id')]) if opening else None
            closing_cash=cash_amount(all_journals) if opening else None
            totals: defaultdict[str,AccountTotal]=defaultdict(lambda:AccountTotal(debit=0,credit=0,journal_ids=[]))
            for j in journals:
                for line in j['lines']:
                    item=totals[line['account']]
                    if line['side']=='debit':item['debit']+=line['amount']
                    else:item['credit']+=line['amount']
                    item['journal_ids'].append(j['id'])
            net=lambda account:totals[account]['debit']-totals[account]['credit']
            cash_in=totals['cash']['debit'];cash_out=totals['cash']['credit']
            from .journal_lineage import internal_entry
            index={j['id']:j for j in all_journals};internal=[j for j in journals if internal_entry(j,index)]
            internal_in=sum(l['amount'] for j in internal for l in j['lines'] if l['account']=='cash' and l['side']=='debit')
            internal_out=sum(l['amount'] for j in internal for l in j['lines'] if l['account']=='cash' and l['side']=='credit')
            cash_in-=internal_in;cash_out-=internal_out
            revenue=-sum(net(account) for account in INCOME_ACCOUNTS);expense=sum(net(account) for account in EXPENSE_ACCOUNTS)
            from .event_settlements import EventSettlements
            payable_coverage=EventSettlements(self.service).report(actor,company,as_of=end,connection=c)
            period={'start':start.isoformat(),'end':end.isoformat()}
            source_revision='sha256:'+digest({
                'company':company,'contract':'price.owner-report/2','period':period,
                'posted_journals':all_journals,
                'originals':source_snapshot(self.db,c,company,{'start':'0001-01-01','end':'9999-12-31'}),
                'economic_events':self.db.list(c,company,'economic_event'),
                'settlements':self.db.list(c,company,'event_settlement'),
                'source_impacts':self.db.list(c,company,'source_change_impact'),
                'crm_candidate_links':self.db.list(c,company,'crm_candidate_link'),
                'crm_company_binding':self.db.list(c,company,'crm_company_binding'),
            })
            if expected_source_revision is not None and expected_source_revision!=source_revision:
                raise DomainError('REPORT_SOURCE_CHANGED','보고 원본이 바뀌었습니다. 최신 결과와 원본을 다시 확인하세요.',409)
            return {'period':period,'basis':'posted_validation_ledger',
                    'company_id':company,'native_contract':'price.owner-report/2','source_revision':source_revision,
                    'read_identity':{'mode':'native_membership_non_worker','role':role,'worker':False},
                    'crm_company_binding':next(iter(self.db.list(c,company,'crm_company_binding')),None),
                    'cash':{'received':cash_in,'paid':cash_out,'net_movement':cash_in-cash_out,'opening_balance':opening_cash,'closing_balance':closing_cash,'opening_balance_verified':bool(opening),'internal_transfers':{'received':internal_in,'paid':internal_out,'net_movement':internal_in-internal_out}},
                    'profit':{'revenue':revenue,'expenses':expense,'net':revenue-expense,'not_cash_balance':True},
                    'account_totals':dict(totals),'journal_ids':[j['id'] for j in journals],
                    'blockers':uploaded_source_blockers(self.db,c,company,{'start':start.isoformat(),'end':end.isoformat()}),
                    'payable_coverage':payable_coverage,
                    'official_tax_ready':False,'company_data_complete':False,'notice':'확정된 검증 장부에서 집계했습니다. 기초잔액과 전체 자료의 완전성은 별도 확인이 필요합니다.'}
