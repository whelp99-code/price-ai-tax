"""Conservative settlement relationships with amount conservation and freshness.

Relationships do not create revenue, costs or cash postings. Automatic links
require a unique explicit invoice reference plus independently matching party.
"""
from __future__ import annotations
from decimal import Decimal
from typing import Annotated
from pydantic import Field,StrictInt,StrictStr
from .db import DomainError,digest
from .upload_models import UploadInput
from .casework import Casework


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
            for bank in (e for e in events if e['kind']=='bank' and e['values'].get('settlement_reference') and e['counterparty_id']):
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

    def report(self,actor,company):
        with self.db.connect() as c:
            self.auth.guard(c,actor,company)
            allocations=[{**a,'current':self._valid(c,company,a)} for a in self.db.list(c,company,'event_settlement')]
            balances=[]
            for event in self.db.list(c,company,'economic_event'):
                if event.get('lifecycle')!='CURRENT' or event['kind'] not in ('invoice','bank'):continue
                try:
                    event=self.casework._fresh_event(c,company,event['id']);gross=exact_krw(event)
                except DomainError:continue
                field='invoice_id' if event['kind']=='invoice' else 'payment_id'
                used=sum(a['amount'] for a in allocations if a['current'] and a[field]==event['id'])
                balances.append({'event_id':event['id'],'kind':event['kind'],'direction':event['direction'],'gross':gross,'allocated':used,'remaining':gross-used,'currency':'KRW'})
            return {'allocations':allocations,'balances':balances,'accounting_posted':False}

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
                for bank in (e for e in events if e['kind']=='bank' and e['values'].get('settlement_reference') and e['counterparty_id']):
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
