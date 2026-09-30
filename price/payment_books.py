"""Turn fully reconciled bank-source events into actual settlement journals.

The bank statement is evidence of money that already moved. This module never
initiates a bank transfer. Receivable/payable settlement cannot book income or
expense for a second time and cannot run on a stale or partially matched source.
"""
from __future__ import annotations
from datetime import date
from typing import Annotated
from pydantic import Field,StrictInt,StrictStr
from .upload_models import UploadInput
from .models import TransactionInput
from .auth import Actor
from .db import DomainError,digest,now
from .casework import Casework
from .event_settlements import EventSettlements,exact_krw


class PaymentRequest(UploadInput):
    payment_event_id:Annotated[StrictStr,Field(min_length=1,max_length=200)]

class PaymentReview(UploadInput):
    expected_revision:Annotated[StrictInt,Field(ge=1)]
    reason:Annotated[StrictStr,Field(min_length=1,max_length=2000)]


class PaymentBooks:
    def __init__(self,service,*,today=None):
        self.service,self.db,self.auth=service,service.db,service.auth
        self.events=Casework(service);self.links=EventSettlements(service);self.today=today or date.today

    def _basis(self,c,company,event_id):
        bank=self.events._fresh_event(c,company,event_id)
        if bank['kind']!='bank' or bank['issues'] or bank['direction'] not in ('receipt','payment'):
            raise DomainError('PAYMENT_SOURCE_UNRESOLVED','정상적으로 확인한 통장 거래가 필요합니다.')
        total=exact_krw(bank)
        if bank['values']['date'][:10]>self.today().isoformat():raise DomainError('PAYMENT_FUTURE','미래 통장 거래를 확정하지 않습니다.')
        self.service._locked(c,company,bank['values']['date'][:10])
        links=[a for a in self.db.list(c,company,'event_settlement') if a['payment_id']==event_id and self.links._valid(c,company,a)]
        if sum(a['amount'] for a in links)!=total:raise DomainError('PAYMENT_NOT_FULLY_MATCHED','통장금액 전부가 검토된 청구·결제 관계로 설명되어야 합니다.')
        journals=self.db.list(c,company,'journal');existing=self.db.list(c,company,'allocation')
        parts=[]
        for link in sorted(links,key=lambda a:a['id']):
            invoice=self.events._fresh_event(c,company,link['invoice_id'])
            posted=[j for j in journals if j['state']=='POSTED' and j.get('economic_event_id')==invoice['id']]
            if len(posted)!=1 or not posted[0].get('transaction_id'):raise DomainError('PAYMENT_INVOICE_NOT_POSTED','연결된 매출·매입의 원장 반영이 먼저 필요합니다.')
            journal=posted[0]
            if any(j.get('reversal_of')==journal['id'] and j['state']=='POSTED' for j in journals):raise DomainError('PAYMENT_INVOICE_REVERSED','원청구가 정정되어 결제 관계를 다시 확인해야 합니다.')
            tx=self.db.get(c,company,'transaction',journal['transaction_id'])
            if tx['revision']!=journal['transaction_revision'] or (bank['direction'],tx['direction']) not in (('receipt','sale'),('payment','purchase')) or tx['currency']!='KRW':
                raise DomainError('PAYMENT_INVOICE_CHANGED','원청구 방향·통화·버전이 맞지 않습니다.')
            used=sum(a['amount'] for a in existing if a['invoice_id']==tx['id'])
            from .card_books import invoice_card_settled
            used+=invoice_card_settled(self.db,c,company,tx['id'])
            planned=sum(a['amount'] for a in parts if a['invoice_transaction_id']==tx['id'])
            if used+planned+link['amount']>tx['gross']:raise DomainError('PAYMENT_INVOICE_OVERPAID','이미 반영한 결제를 포함하면 원청구 금액을 초과합니다.')
            parts.append({'link_id':link['id'],'link_hash':digest(link),'invoice_event_id':invoice['id'],'invoice_hash':invoice['context_hash'],
                          'invoice_journal_id':journal['id'],'invoice_journal_hash':digest(journal),'invoice_transaction_id':tx['id'],'amount':link['amount'],'allocated_before':used})
        if any(j['state']=='POSTED' and j.get('economic_event_id')==bank['id'] for j in journals):raise DomainError('PAYMENT_ALREADY_POSTED','이 통장 거래는 이미 장부에 반영되었습니다.')
        debit='cash' if bank['direction']=='receipt' else 'payable';credit='receivable' if bank['direction']=='receipt' else 'cash'
        evidence=sorted(set(bank['evidence_ids']+[e for part in parts for e in self.db.get(c,company,'journal',part['invoice_journal_id'])['evidence_ids']]))
        return {'event_id':bank['id'],'event_hash':bank['context_hash'],'date':bank['values']['date'][:10],'direction':bank['direction'],
                'counterparty':bank['display_counterparty'],'gross':total,'parts':parts,'evidence_ids':evidence,
                'lines':[{'account':debit,'side':'debit','amount':total},{'account':credit,'side':'credit','amount':total}]}

    def propose(self,actor,company,data:PaymentRequest):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','preparer','reviewer'))
            basis=self._basis(c,company,data.payment_event_id)
            ident='paymentplan_'+digest({'company':company,'basis':basis})
            if c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='payment_plan' AND id=?",(company,ident)).fetchone():return self.db.get(c,company,'payment_plan',ident)
            result=self.db.create(c,company,'payment_plan',{'basis':basis,'author':actor.id,'state':'DRAFT','reviewed_by':None,'review_hash':None,'journal_id':None,'bank_transfer_initiated':False},ident=ident)
            self.db.audit(c,company,actor.id,'payment.book_proposed',ident,{'event_id':data.payment_event_id,'amount':basis['gross']})
            return result

    def approve(self,actor,company,ident,data:PaymentReview):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','reviewer'))
            plan=self.db.get(c,company,'payment_plan',ident)
            if plan['author']==actor.id:raise DomainError('SELF_APPROVAL','결제 전표 작성자와 다른 검토자가 확인해야 합니다.',403)
            self.auth.guard(c,actor,company,('reviewer',))
            if plan['state']!='DRAFT' or plan['revision']!=data.expected_revision:raise DomainError('STALE_VERSION','결제 전표가 변경되었습니다.')
            if not data.reason.strip():raise DomainError('REASON_REQUIRED','대사 결과의 검토 사유가 필요합니다.',422)
            if self._basis(c,company,plan['basis']['event_id'])!=plan['basis']:raise DomainError('PAYMENT_BASIS_CHANGED','원본·기존 결제 금액이 바뀌었습니다.')
            result=self.db.replace(c,company,'payment_plan',ident,{**plan,'state':'APPROVED','reviewed_by':actor.id,'review_hash':digest(plan['basis']),'review_note':data.reason},plan['revision'])
            self.db.audit(c,company,actor.id,'payment.book_approved',ident,{'basis_hash':result['review_hash']})
            return result

    def post(self,actor,company,ident):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','preparer','reviewer'))
            plan=self.db.get(c,company,'payment_plan',ident)
            if plan['state']=='POSTED':return self.db.get(c,company,'journal',plan['journal_id'])
            if plan['state']!='APPROVED' or plan['reviewed_by']==plan['author'] or plan['review_hash']!=digest(plan['basis']):raise DomainError('PAYMENT_NOT_APPROVED','독립 검토한 결제 전표만 반영합니다.')
            self.auth.guard(c,Actor(plan['reviewed_by'],'payment-reviewer'),company,('reviewer',))
            basis=self._basis(c,company,plan['basis']['event_id'])
            if basis!=plan['basis']:raise DomainError('PAYMENT_BASIS_CHANGED','원본·대사·기존 결제가 변경되었습니다.')
            system=Actor('system:payment-books','payment-books')
            tx=self.service._new_tx(c,system,company,TransactionInput(external_id='bank-event-'+basis['event_id'],source='bank',kind='bank',direction=basis['direction'],event_date=basis['date'],counterparty=basis['counterparty'],net=basis['gross'],tax=0,gross=basis['gross'],currency='KRW',business_purpose='원본 청구 참조에 따른 확정 결제',evidence_ids=basis['evidence_ids']))
            for part in basis['parts']:
                self.db.create(c,company,'allocation',{'bank_id':tx['id'],'invoice_id':part['invoice_transaction_id'],'amount':part['amount'],'confirmed_by':plan['reviewed_by'],'source_link_id':part['link_id']})
            self.db.audit(c,company,actor.id,'payment.source_allocated',tx['id'],{'plan':ident,'allocations':len(basis['parts'])})
            journal=self.service._new_journal(c,system,company,{'event_date':basis['date'],'description':'확인된 청구 대금 결제','lines':basis['lines'],'evidence_ids':basis['evidence_ids']},transaction=tx)
            self.service._journal_valid(c,company,journal)
            journal.update(state='POSTED',economic_event_id=basis['event_id'],payment_plan_id=ident,approved_by=plan['reviewed_by'],approval_hash=self.service.journal_hash(journal),authorization_kind='REVIEWED_BANK_SETTLEMENT')
            result=self.db.replace(c,company,'journal',journal['id'],journal,journal['revision'])
            self.db.replace(c,company,'payment_plan',ident,{**plan,'state':'POSTED','journal_id':journal['id'],'posted_at':now()},plan['revision'])
            self.db.audit(c,company,actor.id,'payment.book_posted',journal['id'],{'amount':basis['gross'],'bank_transfer_initiated':False,'plan':ident})
            return result

    def list(self,actor,company):
        with self.db.connect() as c:
            self.auth.guard(c,actor,company)
            return self.db.list(c,company,'payment_plan')
