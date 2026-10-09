"""Conservative settlement relationships with amount conservation and freshness.

Relationships do not create revenue, costs or cash postings. Automatic links
require a unique explicit invoice reference plus independently matching party.
"""
from __future__ import annotations

from contextlib import nullcontext
from datetime import date
from decimal import Decimal
from sqlite3 import Connection
from typing import Annotated, Literal, TypedDict

from pydantic import Field, StrictInt, StrictStr

from .casework import Casework
from .db import DomainError, digest
from .upload_models import UploadInput


class PurchaseObligation(TypedDict):
    po_event_id: str | None
    project_id: str | None
    order_reference: str | None
    payment_terms: str | None
    delivery_status: str | None
    claim_status: str | None
    due_date: str | None
    invoice_event_ids: list[str]
    ordered: int | None
    invoiced: int | None
    unbilled: int | None
    economic_amount: int | None
    invoice_allocated: int
    invoice_remaining: int | None
    advance_outflows: int
    advance_applied: int
    order_payment_outflows: int
    order_payment_applied: int
    fee_outflows: int
    refund_inflows: int
    unresolved_cash_event_ids: list[str]
    unresolved_cash_outflows: int
    net_linked_cash: int
    order_cash_residual: int | None
    source_record_ids: list[str]
    document_ids: list[str]
    evidence_ids: list[str]
    context_hashes: list[str]
    issues: list[str]
    state: str
    payable_today: None
    confirmed: Literal[False]
    cash_coverage_complete: Literal[False]


class SettlementAllocation(TypedDict):
    id: str
    revision: int
    created_at: str
    updated_at: str
    invoice_id: str
    payment_id: str
    invoice_hash: str
    payment_hash: str
    amount: int
    current: bool
    automatic: bool
    reason: str
    request_hash: str
    recorded_by: str
    basis: str
    accounting_posted: bool


class SettlementBalance(TypedDict):
    event_id: str
    kind: str
    direction: str | None
    gross: int
    allocated: int
    remaining: int
    currency: str


class SettlementReport(TypedDict):
    allocations: list[SettlementAllocation]
    balances: list[SettlementBalance]
    purchase_obligations: list[PurchaseObligation]
    accounting_posted: Literal[False]
    confirmed: Literal[False]
    bank_cutoff_verified: Literal[False]
    as_of: str | None
    stale_event_ids: list[str]


class SettlementInput(UploadInput):
    invoice_id: StrictStr
    payment_id: StrictStr
    invoice_hash: Annotated[StrictStr,Field(pattern=r'^[a-f0-9]{64}$')]
    payment_hash: Annotated[StrictStr,Field(pattern=r'^[a-f0-9]{64}$')]
    amount: Annotated[StrictInt,Field(gt=0,le=1_000_000_000_000)]
    idempotency_key: Annotated[StrictStr,Field(pattern=r'^[A-Za-z0-9_-]{8,120}$')]
    reason: Annotated[StrictStr,Field(min_length=1,max_length=500)]


def exact_krw(event):
    if event['values']['currency']!='KRW':raise DomainError('SETTLEMENT_CURRENCY','이 배분 경로는 검증된 원화 거래만 처리합니다.')
    amount=Decimal(event['values']['gross'])
    if amount<=0 or amount!=amount.to_integral_value():raise DomainError('SETTLEMENT_AMOUNT','정상 원화 결제 금액 확인이 필요합니다.')
    return int(amount)


class EventSettlements:
    def __init__(self,service):
        self.db,self.auth,self.casework=service.db,service.auth,Casework(service)

    def _valid(self,c,company,allocation):
        try:
            invoice=self.casework._fresh_event(c,company,allocation['invoice_id'])
            payment=self.casework._fresh_event(c,company,allocation['payment_id'])
            return invoice['context_hash']==allocation['invoice_hash'] and payment['context_hash']==allocation['payment_hash'] and not invoice['issues'] and not payment['issues']
        except DomainError:return False

    def _allocate(self,c,company,actor_id,data,automatic=False):
        key='settlement_'+digest({'company':company,'key':data.idempotency_key})
        request_hash=digest(data.model_dump())
        prior=c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='event_settlement' AND id=?",(company,key)).fetchone()
        if prior:
            old=self.db.get(c,company,'event_settlement',key)
            if old['request_hash']!=request_hash or old['recorded_by']!=actor_id:raise DomainError('SETTLEMENT_KEY_CONFLICT','결제 연결 요청 내용이 달라졌습니다.')
            if not self._valid(c,company,old):raise DomainError('SOURCE_CHANGED','이전 결제 연결의 원본이 변경됐습니다.')
            return {**old,'reused':True}
        invoice=self.casework._fresh_event(c,company,data.invoice_id)
        payment=self.casework._fresh_event(c,company,data.payment_id)
        if invoice['issues'] or payment['issues']:raise DomainError('SOURCE_REVIEW','자료 확인을 먼저 완료해야 합니다.')
        if invoice['context_hash']!=data.invoice_hash or payment['context_hash']!=data.payment_hash:raise DomainError('SOURCE_CHANGED','결제 연결의 자료가 바뀌었습니다.')
        valid_direction=(invoice['direction']=='sale' and payment['direction']=='receipt') or (invoice['direction']=='purchase' and payment['direction']=='payment')
        if invoice['kind']!='invoice' or payment['kind']!='bank' or not valid_direction:raise DomainError('SETTLEMENT_DIRECTION','청구와 입출금의 관계가 맞지 않습니다.')
        if payment['values'].get('cash_kind') in ('fee','refund'):
            raise DomainError('SETTLEMENT_CASH_KIND','수수료와 환급을 청구 대금으로 배분하지 않습니다.')
        gross_invoice,gross_payment=exact_krw(invoice),exact_krw(payment)
        active=[a for a in self.db.list(c,company,'event_settlement') if self._valid(c,company,a)]
        used_invoice=sum(a['amount'] for a in active if a['invoice_id']==invoice['id'])
        used_payment=sum(a['amount'] for a in active if a['payment_id']==payment['id'])
        if data.amount>gross_invoice-used_invoice or data.amount>gross_payment-used_payment:raise DomainError('SETTLEMENT_OVERFLOW','이미 연결된 금액을 포함하면 원본 금액을 초과합니다.')
        body={**data.model_dump(exclude={'idempotency_key'}),'request_hash':request_hash,'recorded_by':actor_id,'automatic':automatic,'basis':'explicit_reference_and_party' if automatic else 'independent_review','accounting_posted':False}
        result=self.db.create(c,company,'event_settlement',body,ident=key)
        self.db.audit(c,company,actor_id,'settlement.recorded',result['id'],{'amount':data.amount,'invoice_id':invoice['id'],'payment_id':payment['id'],'automatic':automatic})
        return {**result,'reused':False}

    def allocate(self,actor,company,data:SettlementInput):
        data=SettlementInput.model_validate(data)
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('reviewer',))
            return self._allocate(c,company,actor.id,data)

    def reconcile_strong_references(self,actor,company):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','preparer','reviewer'))
            events=[]
            for event in self.db.list(c,company,'economic_event'):
                if event.get('lifecycle')!='CURRENT' or event['issues']:continue
                try:events.append(self.casework._fresh_event(c,company,event['id']))
                except DomainError:continue
            linked=[]
            for bank in (e for e in events if e['kind']=='bank' and e['values'].get('cash_kind') in (None,'payment') and e['values'].get('settlement_reference') and e['counterparty_id']):
                candidates=[i for i in events if i['kind']=='invoice' and i['values']['reference']==bank['values']['settlement_reference'] and i['counterparty_id']==bank['counterparty_id'] and i['values']['currency']==bank['values']['currency'] and ((i['direction']=='sale' and bank['direction']=='receipt') or (i['direction']=='purchase' and bank['direction']=='payment'))]
                if len(candidates)!=1:continue
                invoice=candidates[0]
                try:limit_invoice,limit_bank=exact_krw(invoice),exact_krw(bank)
                except DomainError:continue
                active=[a for a in self.db.list(c,company,'event_settlement') if self._valid(c,company,a)]
                rem_i=limit_invoice-sum(a['amount'] for a in active if a['invoice_id']==invoice['id'])
                rem_b=limit_bank-sum(a['amount'] for a in active if a['payment_id']==bank['id'])
                # A remainder larger than the invoice needs a genuine multi-invoice allocation, not a guess.
                if not 0<rem_b<=rem_i:continue
                key='auto_'+digest({'invoice':invoice['id'],'ih':invoice['context_hash'],'bank':bank['id'],'bh':bank['context_hash']})
                data=SettlementInput(invoice_id=invoice['id'],payment_id=bank['id'],invoice_hash=invoice['context_hash'],payment_hash=bank['context_hash'],amount=rem_b,idempotency_key=key,reason='원본의 청구 참조번호와 거래 상대 식별이 일치')
                linked.append(self._allocate(c,company,'system:settlement-linker',data,automatic=True)['id'])
            return {'linked_ids':linked,'new_links':len(linked),'accounting_posted':False}

    def report(self,actor,company,as_of:date|None=None,*,connection:Connection|None=None) -> SettlementReport:
        with nullcontext(connection) if connection is not None else self.db.connect() as c:
            if connection is None:
                c.execute('BEGIN')
            self.auth.guard(c,actor,company)
            raw_events=self.db.list(c,company,'economic_event')
            context=self.casework._source_context(c,company) if raw_events else None
            events=[]
            stale=[]
            for event in raw_events:
                if event.get('lifecycle')!='CURRENT':continue
                if as_of is not None and str(event['values'].get('date') or '')[:10]>as_of.isoformat():continue
                try:events.append(self.casework._fresh_event(c,company,event['id'],context))
                except DomainError:stale.append(event['id'])
            eligible={event['id'] for event in events}
            allocations: list[SettlementAllocation]=[]
            for allocation in self.db.list(c,company,'event_settlement'):
                allocations.append(SettlementAllocation(
                    id=allocation['id'],revision=allocation['revision'],created_at=allocation['created_at'],updated_at=allocation['updated_at'],
                    invoice_id=allocation['invoice_id'],payment_id=allocation['payment_id'],
                    invoice_hash=allocation['invoice_hash'],payment_hash=allocation['payment_hash'],
                    amount=allocation['amount'],automatic=allocation['automatic'],reason=allocation['reason'],
                    request_hash=allocation['request_hash'],recorded_by=allocation['recorded_by'],
                    basis=allocation['basis'],accounting_posted=allocation['accounting_posted'],
                    current=self._valid(c,company,allocation) and allocation['invoice_id'] in eligible and allocation['payment_id'] in eligible))
            balances: list[SettlementBalance]=[]
            for event in events:
                if event['kind'] not in ('invoice','bank'):continue
                try:
                    event=self.casework._fresh_event(c,company,event['id']);gross=exact_krw(event)
                except DomainError:continue
                field='invoice_id' if event['kind']=='invoice' else 'payment_id'
                used=sum(a['amount'] for a in allocations if a['current'] and a[field]==event['id'])
                balances.append({'event_id':event['id'],'kind':event['kind'],'direction':event['direction'],'gross':gross,'allocated':used,'remaining':gross-used,'currency':'KRW'})
            orders=[e for e in events if e['kind']=='purchase_order' and e['direction']=='purchase' and e['values'].get('order_status')=='issued']
            invoices=[e for e in events if e['kind']=='invoice' and e['direction']=='purchase']
            banks={e['id']:e for e in events if e['kind']=='bank'}
            invoice_groups={order['id']:[] for order in orders}
            unmatched=[]
            for invoice in invoices:
                v=invoice['values']
                candidates=[o for o in orders if v.get('order_reference') and v.get('project_id')
                            and v['order_reference']==o['values']['reference'] and v['project_id']==o['values'].get('project_id')
                            and v['supplier_id']==o['values']['supplier_id'] and v['buyer_id']==o['values']['buyer_id']
                            and v['currency']==o['values']['currency']]
                if len(candidates)==1:invoice_groups[candidates[0]['id']].append(invoice)
                else:unmatched.append(invoice)
            obligations: list[PurchaseObligation]=[]
            groups=[(o,invoice_groups[o['id']]) for o in orders]+[(None,[i]) for i in unmatched]
            for order,group in groups:
                basis=[order] if order else group
                issues={issue for event in basis+group for issue in event['issues']}
                ordered=None
                if order and not order['issues']:
                    try:ordered=exact_krw(order)
                    except DomainError:issues.add('ORDER_AMOUNT_REVIEW')
                invoice_amounts=[]
                for invoice in group:
                    if invoice['issues']:invoice_amounts.append(None);continue
                    try:invoice_amounts.append(exact_krw(invoice))
                    except DomainError:invoice_amounts.append(None);issues.add('INVOICE_AMOUNT_REVIEW')
                invoiced=sum(amount for amount in invoice_amounts if amount is not None) if all(amount is not None for amount in invoice_amounts) else None
                invoice_ids={i['id'] for i in group}
                active=[a for a in allocations if a['current'] and a['invoice_id'] in invoice_ids and a['payment_id'] in banks]
                allocated=sum(a['amount'] for a in active)
                cash=[]
                if order:
                    ov=order['values']
                    cash=[b for b in banks.values() if b['values'].get('order_reference')==ov['reference']
                          and b['values'].get('project_id')==ov.get('project_id') and b['counterparty_id']==order['counterparty_id']]
                advance=fee=refund=advance_applied=order_payment=order_payment_applied=0
                unresolved_cash_event_ids: list[str]=[]
                unresolved_cash_outflows=0
                for bank in cash:
                    if bank['issues']:issues.update(bank['issues']);continue
                    try:amount=exact_krw(bank)
                    except DomainError:issues.add('CASH_AMOUNT_REVIEW');continue
                    match bank['values'].get('cash_kind'):
                        case 'advance' if bank['direction']=='payment':
                            advance+=amount
                            advance_applied+=sum(a['amount'] for a in allocations if a['current'] and a['payment_id']==bank['id'])
                        case 'payment' if bank['direction']=='payment':
                            order_payment+=amount
                            order_payment_applied+=sum(a['amount'] for a in allocations if a['current'] and a['payment_id']==bank['id'])
                        case 'fee' if bank['direction']=='payment':fee+=amount
                        case 'refund' if bank['direction']=='receipt':refund+=amount
                        case None | '' if bank['direction']=='payment':
                            unresolved_cash_event_ids.append(bank['id'])
                            unresolved_cash_outflows+=amount
                            issues.add('CASH_CLASSIFICATION_REVIEW')
                        case _:
                            if bank['values'].get('cash_kind') not in (None,'payment'):
                                issues.add('CASH_DIRECTION_REVIEW')
                net_cash=allocated+advance-advance_applied+order_payment-order_payment_applied-refund
                unbilled=ordered-invoiced if ordered is not None and invoiced is not None else None
                residual=ordered-net_cash if ordered is not None and not unresolved_cash_event_ids else None
                if unbilled is not None and unbilled<0:issues.add('INVOICES_EXCEED_ORDER')
                if residual is not None and residual<0:issues.add('CASH_EXCEEDS_ORDER')
                if stale:issues.add('STALE_SOURCE_EXCLUDED')
                state='SOURCE_REVIEW' if issues else 'UNBILLED_ORDER' if order and not group else 'PARTIALLY_BILLED_ORDER' if unbilled else 'INVOICE_COVERED'
                linked=basis+group+cash+[banks[a['payment_id']] for a in active]
                obligations.append({
                    'po_event_id':order['id'] if order else None,
                    'project_id':basis[0]['values'].get('project_id'),
                    'order_reference':order['values']['reference'] if order else basis[0]['values'].get('order_reference'),
                    'payment_terms':order['values'].get('payment_terms') if order else None,
                    'delivery_status':order['values'].get('delivery_status') if order else None,
                    'claim_status':order['values'].get('claim_status') if order else None,
                    'due_date':order['values'].get('due_date') if order else None,
                    'invoice_event_ids':sorted(invoice_ids),'ordered':ordered,'invoiced':invoiced,'unbilled':unbilled,
                    'economic_amount':ordered if order else invoiced,'invoice_allocated':allocated,
                    'invoice_remaining':invoiced-allocated if invoiced is not None else None,
                    'advance_outflows':advance,'advance_applied':advance_applied,'fee_outflows':fee,'refund_inflows':refund,
                    'order_payment_outflows':order_payment,'order_payment_applied':order_payment_applied,
                    'unresolved_cash_event_ids':sorted(unresolved_cash_event_ids),
                    'unresolved_cash_outflows':unresolved_cash_outflows,
                    'net_linked_cash':net_cash,'order_cash_residual':residual,
                    'source_record_ids':sorted({s for e in linked for s in e['source_record_ids']}),
                    'document_ids':sorted({s for e in linked for s in e['document_ids']}),
                    'evidence_ids':sorted({s for e in linked for s in e['evidence_ids']}),
                    'context_hashes':sorted({e['context_hash'] for e in linked}),
                    'issues':sorted(issues),'state':state,'payable_today':None,'confirmed':False,'cash_coverage_complete':False,
})
            return {'allocations':allocations,'balances':balances,'purchase_obligations':obligations,
                    'accounting_posted':False,'confirmed':False,'bank_cutoff_verified':False,'as_of':as_of.isoformat() if as_of else None,
                    'stale_event_ids':sorted(stale)}

    def run_once(self,company=None):
        """Internal proposal worker: only explicit-reference relationship effects."""
        with self.db.connect() as c:
            companies=[r[0] for r in c.execute('SELECT id FROM companies'+(' WHERE id=?' if company else '')+' ORDER BY id',([company] if company else []))]
        for company_id in companies:
            with self.db.transaction() as c:
                events=[]
                for event in self.db.list(c,company_id,'economic_event'):
                    if event.get('lifecycle')!='CURRENT' or event['issues']:continue
                    try:events.append(self.casework._fresh_event(c,company_id,event['id']))
                    except DomainError:continue
                for bank in (e for e in events if e['kind']=='bank' and e['values'].get('cash_kind') in (None,'payment') and e['values'].get('settlement_reference') and e['counterparty_id']):
                    invoices=[i for i in events if i['kind']=='invoice' and i['values']['reference']==bank['values']['settlement_reference'] and i['counterparty_id']==bank['counterparty_id'] and i['values']['currency']==bank['values']['currency'] and ((i['direction']=='sale' and bank['direction']=='receipt') or (i['direction']=='purchase' and bank['direction']=='payment'))]
                    if len(invoices)!=1:continue
                    invoice=invoices[0]
                    try:iv,bv=exact_krw(invoice),exact_krw(bank)
                    except DomainError:continue
                    active=[a for a in self.db.list(c,company_id,'event_settlement') if self._valid(c,company_id,a)]
                    ri=iv-sum(a['amount'] for a in active if a['invoice_id']==invoice['id'])
                    rb=bv-sum(a['amount'] for a in active if a['payment_id']==bank['id'])
                    if not 0<rb<=ri:continue
                    key='auto_'+digest({'invoice':invoice['id'],'ih':invoice['context_hash'],'bank':bank['id'],'bh':bank['context_hash']})
                    data=SettlementInput(invoice_id=invoice['id'],payment_id=bank['id'],invoice_hash=invoice['context_hash'],payment_hash=bank['context_hash'],amount=rb,idempotency_key=key,reason='원본의 청구 참조번호와 거래 상대 식별이 일치')
                    result=self._allocate(c,company_id,'system:settlement-linker',data,automatic=True)
                    return {'processed':True,'state':'LINKED_NOT_POSTED','id':result['id']}
        return {'processed':False}
