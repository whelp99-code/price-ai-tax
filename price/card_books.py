"""Source-backed card debt lifecycle: charge, cancellation, payment and refund.

No network or payment execution exists here. A card charge settling an existing
purchase debits supplier debt, not expense again. Tax treatment remains on the
original reviewed invoice. Explicit unambiguous references and independent
review are required; missing receipt/merchant/card evidence is never invented.
"""
from __future__ import annotations
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from pydantic import Field, StrictStr, StrictInt, model_validator
from .db import DomainError,digest,now
from .auth import Actor
from .upload_models import UploadInput
from .casework import Casework
from .internal_transfers import source_contributors


class CardBookInput(UploadInput):
    operation:Literal['invoice_charge','cancellation','billing']
    source_event_id:Annotated[StrictStr,Field(min_length=1,max_length=200)]
    invoice_event_id:Annotated[StrictStr,Field(max_length=200)]|None=None
    prior_plan_id:Annotated[StrictStr,Field(max_length=200)]|None=None
    statement_plan_ids:Annotated[list[Annotated[StrictStr,Field(min_length=1,max_length=200)]],Field(max_length=1000)]=[]
    @model_validator(mode='after')
    def scope(self):
        if self.operation=='invoice_charge' and (not self.invoice_event_id or self.prior_plan_id or self.statement_plan_ids):raise ValueError('매입-카드 연결 대상이 필요합니다.')
        if self.operation=='cancellation' and (not self.prior_plan_id or self.invoice_event_id or self.statement_plan_ids):raise ValueError('원승인 대상을 지정해야 합니다.')
        if self.operation=='billing' and (not self.statement_plan_ids or self.prior_plan_id or self.invoice_event_id):raise ValueError('대금 내역을 지정해야 합니다.')
        if len(self.statement_plan_ids)!=len(set(self.statement_plan_ids)):raise ValueError('결제 내역이 중복되었습니다.')
        return self


class CardBookReview(UploadInput):
    expected_revision:Annotated[StrictInt,Field(ge=1)]
    reason:Annotated[StrictStr,Field(min_length=1,max_length=2000)]


def _money(event,positive=True):
    if event['values']['currency']!='KRW':raise DomainError('CARD_CURRENCY','같은 원화 거래끼리만 자동 대사합니다.')
    value=Decimal(event['values']['gross'])
    if value!=value.to_integral_value() or abs(value)>1_000_000_000_000 or not value or (positive and value<0):raise DomainError('CARD_AMOUNT','원금액·취소 여부를 확인해야 합니다.')
    return int(value)


class CardBooks:
    def __init__(self,service,*,today=None):
        self.service,self.db,self.auth=service,service.db,service.auth
        self.work=Casework(service);self.today=today or date.today

    def linked_invoice(self,c,company,event,context=None):
        if event['kind']!='card' or event['issues'] or event['ownership']!='owner_confirmed':return None
        v=event['values']
        if not v.get('settlement_reference') or not event['counterparty_id']:return None
        candidates=[];context=context or self.work._source_context(c,company)
        for raw in self.db.list(c,company,'economic_event'):
            if raw['kind']!='invoice' or raw.get('lifecycle')!='CURRENT':continue
            try:invoice=self.work._fresh_event(c,company,raw['id'],context)
            except DomainError:continue
            if invoice['issues'] or invoice['direction']!='purchase' or invoice['ownership']!='owner_confirmed':continue
            if invoice['values']['reference']==v['settlement_reference'] and invoice['counterparty_id']==event['counterparty_id'] and invoice['values']['currency']==v['currency']:
                candidates.append(invoice)
        return candidates[0] if len(candidates)==1 else None

    def run_once(self,company=None):
        with self.db.transaction() as c:
            companies=[r[0] for r in c.execute('SELECT id FROM companies'+(' WHERE id=?' if company else ''),[company] if company else [])]
            for company_id in companies:
                plans=self.db.list(c,company_id,'card_book');known={p['id'] for p in plans};inputs=[]
                events=[e for e in self.db.list(c,company_id,'economic_event') if e.get('lifecycle')=='CURRENT']
                context=self.work._source_context(c,company_id)
                for e in events:
                    if e['kind']=='card' and not e['issues']:
                        inv=self.linked_invoice(c,company_id,e,context)
                        if inv:inputs.append(CardBookInput(operation='invoice_charge',source_event_id=e['id'],invoice_event_id=inv['id']))
                    elif e['kind']=='card' and e['values'].get('original_reference'):
                        matches=[]
                        for p in plans:
                            if p['state']!='POSTED' or p['input']['operation']!='invoice_charge':continue
                            try:_,original,_=self._posted_plan(c,company_id,p['id'])
                            except DomainError:continue
                            if original['values']['reference']==e['values']['original_reference'] and original['values']['card']==e['values']['card'] and original['counterparty_id']==e['counterparty_id']:matches.append(p)
                        if len(matches)==1:inputs.append(CardBookInput(operation='cancellation',source_event_id=e['id'],prior_plan_id=matches[0]['id']))
                    elif e['kind']=='bank' and e['values'].get('card'):
                        claimed={i for p in plans if p['state']=='POSTED' and p['input']['operation']=='billing' for i in p['input']['statement_plan_ids']}
                        due=[p for p in plans if p['state']=='POSTED' and p['input']['operation']!='billing' and p['id'] not in claimed and p['basis']['card']==e['values']['card'] and p['basis']['date']<=e['values']['date'][:10]]
                        if due:inputs.append(CardBookInput(operation='billing',source_event_id=e['id'],statement_plan_ids=sorted(p['id'] for p in due)))
                for data in inputs:
                    try:proposal=self._propose(c,company_id,data,'system:card-proposals')
                    except DomainError:continue
                    if proposal['id'] not in known:return {'processed':True,'state':'PROPOSED','operation':data.operation}
        return {'processed':False}

    def _event(self,c,company,ident,*,correction=False):
        e=self.work._fresh_event(c,company,ident)
        allowed={'CORRECTION_REVIEW'} if correction else set()
        if set(e['issues'])-allowed or e['ownership']!='owner_confirmed':raise DomainError('CARD_SOURCE_SCOPE','회사 소유·통화·출처가 명확한 카드와 계좌 자료가 필요합니다.')
        if e['values']['date'][:10]>self.today().isoformat():raise DomainError('CARD_FUTURE','미래 원본 거래를 미리 확정하지 않습니다.')
        return e

    def _original_posted(self,c,company,event):
        posted=[j for j in self.db.list(c,company,'journal') if j['state']=='POSTED' and j.get('economic_event_id')==event['id']]
        if len(posted)!=1:raise DomainError('CARD_PURCHASE_NOT_POSTED','원매입을 먼저 검토하고 장부에 반영해야 합니다.')
        j=posted[0]
        if any(x['state']=='POSTED' and x.get('reversal_of')==j['id'] for x in self.db.list(c,company,'journal')):raise DomainError('CARD_SOURCE_REVERSED','정정된 원매입/승인 내역을 다시 대조해야 합니다.')
        return j

    def _posted_plan(self,c,company,ident):
        p=self.db.get(c,company,'card_book',ident)
        if p['state']!='POSTED':raise DomainError('CARD_PRIOR_NOT_POSTED','원승인 내역의 장부 반영이 필요합니다.')
        j=self.db.get(c,company,'journal',p['journal_id'])
        if j['state']!='POSTED' or j.get('card_book_id')!=ident:raise DomainError('CARD_PRIOR_INTEGRITY','원승인 원장 연결이 일치하지 않습니다.')
        source=self._event(c,company,p['input']['source_event_id'],correction=p['input']['operation']=='cancellation')
        if source['context_hash']!=p['basis']['source_hash']:raise DomainError('CARD_PRIOR_SOURCE_CHANGED','원승인/취소 자료가 다시 읽히거나 정정되었습니다.')
        for ref in p['basis']['references']:
            if ref.get('invoice_id'):
                invoice=self._event(c,company,ref['invoice_id'])
                if invoice['context_hash']!=ref['hash'] or digest(self._original_posted(c,company,invoice))!=ref['journal_hash']:
                    raise DomainError('CARD_INVOICE_CHANGED','원매입과 그 카드 결제 관계를 다시 검토해야 합니다.')
        if any(x['state']=='POSTED' and x.get('reversal_of')==j['id'] for x in self.db.list(c,company,'journal')):raise DomainError('CARD_PRIOR_REVERSED','정정된 카드 원장을 먼저 확인해야 합니다.')
        return p,source,j

    def _basis(self,c,company,data):
        source=self._event(c,company,data.source_event_id,correction=data.operation=='cancellation')
        self.service._locked(c,company,source['values']['date'][:10])
        if any(j['state']=='POSTED' and j.get('economic_event_id')==source['id'] for j in self.db.list(c,company,'journal')):raise DomainError('CARD_EVENT_ALREADY_POSTED','이미 반영된 원본 거래입니다.')
        events=[source];refs=[];plans=self.db.list(c,company,'card_book');currency='KRW';invoice_id=None
        value=source['values'];card=value.get('card');amount=_money(source,positive=data.operation!='cancellation')
        if data.operation=='invoice_charge':
            invoice=self._event(c,company,data.invoice_event_id)
            if source['kind']!='card' or invoice['kind']!='invoice' or invoice['direction']!='purchase':raise DomainError('CARD_PURCHASE_DIRECTION','회사 매입과 카드 사용의 방향이 맞지 않습니다.')
            if not value.get('settlement_reference') or value['settlement_reference']!=invoice['values']['reference'] or not source['counterparty_id'] or source['counterparty_id']!=invoice['counterparty_id']:
                raise DomainError('CARD_REFERENCE_REQUIRED','가맹점과 원청구 참조를 원본에서 대조해야 합니다. 금액만 같다고 연결하지 않습니다.')
            if value['date'][:10]<invoice['values']['date'][:10]:raise DomainError('CARD_DATE_ORDER','선결제는 별도 검토 대상입니다.')
            original=self._original_posted(c,company,invoice)
            if not original.get('transaction_id'):raise DomainError('CARD_INVOICE_TRANSACTION','원매입 거래 연결이 없습니다.')
            tx=self.db.get(c,company,'transaction',original['transaction_id'])
            if tx['revision']!=original.get('transaction_revision') or tx['direction']!='purchase':raise DomainError('CARD_INVOICE_CHANGED','매입 거래의 원본 버전이 달라졌습니다.')
            payable=sum(l['amount']*(1 if l['side']=='credit' else -1) for l in original['lines'] if l['account']=='payable')
            already=sum(a['amount'] for a in self.db.list(c,company,'allocation') if a['invoice_id']==tx['id'])
            card_used=invoice_card_settled(self.db,c,company,tx['id'])
            if payable!=tx['gross'] or amount+already+card_used>payable:raise DomainError('CARD_INVOICE_OVERSETTLED','기존 은행/카드 결제를 더하면 원매입 금액을 초과합니다.')
            events.append(invoice);invoice_id=tx['id'];signed=amount
            refs=[{'invoice_id':invoice['id'],'hash':invoice['context_hash'],'journal':original['id'],'journal_hash':digest(original),'used':already+card_used}]
            lines=[{'account':'payable','side':'debit','amount':amount},{'account':'card_payable','side':'credit','amount':amount}]
        elif data.operation=='cancellation':
            prior,old,j=self._posted_plan(c,company,data.prior_plan_id)
            if prior['input']['operation']!='invoice_charge' or source['kind']!='card' or old['kind']!='card':raise DomainError('CARD_CANCEL_TARGET','원카드 승인에 대한 취소만 연결합니다.')
            if not value.get('original_reference') or value['original_reference']!=old['values']['reference'] or value['card']!=old['values']['card'] or source['counterparty_id']!=old['counterparty_id']:
                raise DomainError('CARD_CANCEL_REFERENCE','카드·가맹점·원승인번호가 맞지 않습니다.')
            if not source['counterparty_id'] or value['date'][:10]<old['values']['date'][:10]:raise DomainError('CARD_CANCEL_SOURCE','원승인과 취소 일자/가맹점을 대조해야 합니다.')
            if amount>0 and not any(x in str(value.get('status','')).lower() for x in ('취소','cancel')):raise DomainError('CARD_CANCEL_SIGN','취소 표기와 금액 방향을 확인해야 합니다.')
            amount=abs(amount)
            used=sum(p['basis']['amount'] for p in plans if p['state']=='POSTED' and p['input']['operation']=='cancellation' and p['input']['prior_plan_id']==prior['id'])
            if used+amount>prior['basis']['amount']:raise DomainError('CARD_CANCEL_OVERRUN','취소 합계가 원승인 금액을 초과합니다.')
            events.append(old);invoice_id=prior['basis']['invoice_transaction_id'];signed=-amount
            refs=[{'prior_plan':prior['id'],'hash':digest(prior),'journal':j['id'],'used':used}]
            lines=[{'account':'card_payable','side':'debit','amount':amount},{'account':'payable','side':'credit','amount':amount}]
        else:
            if source['kind']!='bank' or not card or any(x in card for x in ('*','X')):raise DomainError('CARD_BILL_BANK','카드번호가 명시된 회사 통장 원본이 필요합니다.')
            claims={x for p in plans if p['state']=='POSTED' and p['input']['operation']=='billing' for x in p['input']['statement_plan_ids']}
            signed=0
            for ident in sorted(data.statement_plan_ids):
                if ident in claims:raise DomainError('CARD_BILL_ALREADY_USED','이미 납부/환급에 연결한 카드 내역입니다.')
                p,e,j=self._posted_plan(c,company,ident)
                if p['input']['operation'] not in ('invoice_charge','cancellation') or p['basis']['card']!=card:raise DomainError('CARD_BILL_CARD','같은 카드의 미정산 사용/취소 내역이어야 합니다.')
                if e['values']['date'][:10]>value['date'][:10]:raise DomainError('CARD_BILL_DATE','납부 이후의 사용 내역을 미리 결제 처리하지 않습니다.')
                signed+=p['basis']['signed_card_debt'];events.append(e);refs.append({'plan_id':p['id'],'hash':digest(p),'journal_id':j['id']})
            if not signed or abs(signed)!=amount:raise DomainError('CARD_BILL_TOTAL','사용·취소 순액과 통장 금액이 일치하지 않습니다.')
            if (signed>0 and source['direction']!='payment') or (signed<0 and source['direction']!='receipt'):raise DomainError('CARD_BILL_DIRECTION','카드 납부/환급 방향이 맞지 않습니다.')
            facts=self.work._answers(c,company,source)
            if facts.get('cash_reason',{}).get('answer') not in (None,'card_payment','refund'):raise DomainError('CARD_BILL_FACTS','대표가 확인한 입출금 목적과 충돌합니다.')
            lines=([{'account':'card_payable','side':'debit','amount':amount},{'account':'cash','side':'credit','amount':amount}] if signed>0 else [{'account':'cash','side':'debit','amount':amount},{'account':'card_payable','side':'credit','amount':amount}])
            signed=-signed
        evidence=sorted({item for e in events for item in e['evidence_ids']})
        self.service._evidence_check(c,company,evidence,required=True)
        return {'operation':data.operation,'source_event_id':source['id'],'source_hash':source['context_hash'],'date':value['date'][:10],
                'amount':amount,'signed_card_debt':signed,'card':card,'currency':currency,'invoice_transaction_id':invoice_id,
                'references':refs,'evidence_ids':evidence,'contributor_ids':sorted(source_contributors(self.db,c,company,events)),
                'lines':lines,'facts_hash':digest({e['id']:self.work._answers(c,company,e) for e in events}),
                'tax_recomputed':False,'additional_expense_created':False,'bank_transfer_initiated':False}

    def _propose(self,c,company,data,author):
        basis=self._basis(c,company,data);body=data.model_dump(mode='json')
        ident='cardbook_'+digest({'company':company,'input':body,'basis':basis})
        if c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='card_book' AND id=?",(company,ident)).fetchone():return self.db.get(c,company,'card_book',ident)
        p=self.db.create(c,company,'card_book',{'input':body,'basis':basis,'author':author,'state':'DRAFT','reviewed_by':None,'review_hash':None,'journal_id':None},ident=ident)
        self.db.audit(c,company,author,'card.proposed',ident,{'operation':data.operation,'source_event':data.source_event_id})
        return p

    def propose(self,actor,company,data:CardBookInput):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','preparer','reviewer'))
            return self._propose(c,company,data,actor.id)

    def approve(self,actor,company,ident,data:CardBookReview):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('reviewer',))
            p=self.db.get(c,company,'card_book',ident)
            if p['state']!='DRAFT' or p['revision']!=data.expected_revision:raise DomainError('STALE_VERSION','검토 대상이 바뀌었습니다.')
            current=self._basis(c,company,CardBookInput(**p['input']))
            if current!=p['basis']:raise DomainError('CARD_BASIS_CHANGED','원매입·카드·기존 납부 내역이 바뀌었습니다.')
            if actor.id in set(current['contributor_ids'])|{p['author']}:raise DomainError('SELF_APPROVAL','원본과 사실 작성자와 다른 담당자의 확인이 필요합니다.',403)
            if not data.reason.strip():raise DomainError('REASON_REQUIRED','대사 근거가 필요합니다.',422)
            p=self.db.replace(c,company,'card_book',ident,{**p,'state':'APPROVED','reviewed_by':actor.id,'review_hash':digest(current),'review_note':data.reason},p['revision'])
            self.db.audit(c,company,actor.id,'card.approved',ident,{'basis_hash':p['review_hash']})
            return p

    def post(self,actor,company,ident):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','preparer','reviewer'))
            p=self.db.get(c,company,'card_book',ident)
            if p['state']=='POSTED':return self.db.get(c,company,'journal',p['journal_id'])
            if p['state']!='APPROVED' or p['review_hash']!=digest(p['basis']):raise DomainError('CARD_NOT_APPROVED','독립 검토한 대사만 반영합니다.')
            self.auth.guard(c,Actor(p['reviewed_by'],'card-reviewer'),company,('reviewer',))
            basis=self._basis(c,company,CardBookInput(**p['input']))
            if basis!=p['basis']:raise DomainError('CARD_BASIS_CHANGED','원본 또는 카드채무가 변경되었습니다.')
            if p['reviewed_by'] in set(basis['contributor_ids'])|{p['author']}:raise DomainError('SELF_APPROVAL','독립 검토 조건이 바뀌었습니다.',403)
            j=self.service._new_journal(c,Actor('system:card-books','card-books'),company,{'event_date':basis['date'],'description':'확인된 카드 '+basis['operation'],'lines':basis['lines'],'evidence_ids':basis['evidence_ids']})
            self.service._journal_valid(c,company,j)
            j.update(state='POSTED',approved_by=p['reviewed_by'],approval_hash=self.service.journal_hash(j),economic_event_id=basis['source_event_id'],card_book_id=ident,
                     card_instrument=basis['card'],authorization_kind='REVIEWED_CARD_LIFECYCLE',card_basis_hash=p['review_hash'])
            j=self.db.replace(c,company,'journal',j['id'],j,j['revision'])
            self.db.replace(c,company,'card_book',ident,{**p,'state':'POSTED','journal_id':j['id'],'posted_at':now()},p['revision'])
            self.db.audit(c,company,actor.id,'card.booked',j['id'],{'plan':ident,'operation':basis['operation'],'bank_transfer_initiated':False})
            return j

    def list(self,actor,company):
        with self.db.connect() as c:
            self.auth.guard(c,actor,company);c.execute('BEGIN')
            plans=self.db.list(c,company,'card_book');balances={};items=[]
            from .journal_lineage import card_instrument
            journals={j['id']:j for j in self.db.list(c,company,'journal') if j['state']=='POSTED'}
            for journal in journals.values():
                card=card_instrument(journal,journals)
                if card:
                    balances[card]=balances.get(card,0)+sum(l['amount']*(1 if l['side']=='credit' else -1) for l in journal['lines'] if l['account']=='card_payable')
            for p in plans:
                b=p['basis'];stale=False
                if p['state']=='POSTED':
                    try:self._posted_plan(c,company,p['id'])
                    except DomainError:stale=True
                items.append({'id':p['id'],'revision':p['revision'],'operation':p['input']['operation'],'state':p['state'],'date':b['date'],'amount':b['amount'],'card':b['card'],'source_event_id':b['source_event_id'],'journal_id':p['journal_id'],'source_review_needed':stale})
            return {'items':items,'balances':balances,'bank_transfer_initiated':False,'notice':'원매입과 카드채무·납부를 분리합니다. 카드 대금은 새로운 비용으로 중복 기록하지 않습니다.'}


def invoice_card_settled(db,c,company,transaction_id):
    reversed_ids={j['reversal_of'] for j in db.list(c,company,'journal') if j['state']=='POSTED' and j.get('reversal_of')}
    return sum(p['basis']['signed_card_debt'] for p in db.list(c,company,'card_book') if p['state']=='POSTED' and p['journal_id'] not in reversed_ids and p['basis'].get('invoice_transaction_id')==transaction_id and p['input']['operation'] in ('invoice_charge','cancellation'))


def is_accounted_card_correction(db,c,company,event,journal):
    if set(event['issues'])!={'CORRECTION_REVIEW'} or journal.get('authorization_kind')!='REVIEWED_CARD_LIFECYCLE':return False
    ident=journal.get('card_book_id')
    if not ident:return False
    p=db.get(c,company,'card_book',ident)
    return p['state']=='POSTED' and p['input']['operation']=='cancellation' and p['journal_id']==journal['id'] and p['basis']['source_hash']==event['context_hash']
