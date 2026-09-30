"""Invoice + card + bank examples must not multiply expense or input VAT."""
from datetime import date
from concurrent.futures import ThreadPoolExecutor
import pytest
from price.card_books import CardBooks,CardBookInput,CardBookReview
from price.autopolicies import PolicyEngine,PolicyInput,PolicyReview,PolicyTruthCase,PolicyAction
from price.models import AuthorityInput
from price.owner import OwnerView,CompanyFactsInput
from price.ingestion import Ingestion
from price.casework import Casework
from price.db import DomainError
from price.extractions import Extractions,ReprocessInput
from test_casework import answer
from test_source_ingestion import upload,INVOICES

CARD='1111222233334444';BANK='123456789012'
CARD_HEADER='승인일자,카드번호,가맹점명,승인금액,승인번호,승인취소구분,통화,청구참조번호,상대방사업자등록번호,당초승인번호\n'
BANK_HEADER='거래일자,거래내용,입금액,출금액,계좌번호,카드번호,통화,거래번호\n'
EXPECTED=[{'account':'expense','side':'debit','amount':1000},{'account':'vat_input','side':'debit','amount':100},{'account':'payable','side':'credit','amount':1100}]


def read(w,raw,key):
    upload(w,raw,key=key);reader=Ingestion(w.svc);reader.run_once(w.company);work=Casework(w.svc);work.refresh(w.owner,w.company)
    return work


def setup(w,*,reference='SYNTHETIC-01',merchant='1111111111'):
    reader=Ingestion(w.svc)
    OwnerView(w.svc,reader.uploads).confirm(w.owner,w.company,CompanyFactsInput(expected_revision=0,name='합성 회사',business_number='2222222222',mode='company',confirmed=True,instruments=[{'kind':'bank','identifier':BANK,'currency':'KRW'},{'kind':'card','identifier':CARD,'currency':'KRW'}]))
    work=read(w,INVOICES,'card-invoice-source')
    invoice=work.list(w.owner,w.company)[0]
    answer(work,w,invoice,'business_use','business');answer(work,w,invoice,'use_type','operations',key='card-use-purpose')
    a=w.svc.add_authority(w.owner,w.company,AuthorityInput(title='SYNTHETIC internal fixture policy',url='https://www.law.go.kr',effective_from='2026-01-01',effective_to='2026-12-31',excerpt='Synthetic reviewed test fixture, not a tax or professional signoff.',kind='sample'))
    w.svc.review_authority(w.reviewer,w.company,a['id'],a['revision'],'합성 정답 검증')
    policy_engine=PolicyEngine(w.svc,posting_enabled=True)
    policy=policy_engine.create(w.owner,w.company,PolicyInput(name='합성 매입',direction='purchase',counterparties=['1111111111'],tax_category='standard',deductible=True,amount_limit=10000,effective_from=date(2026,1,1),effective_to=date(2026,12,31),authority_ids=[a['id']]))
    policy=policy_engine.review(w.reviewer,w.company,policy['id'],PolicyReview(expected_revision=policy['revision'],cases=[PolicyTruthCase(event_id=invoice['id'],expected_lines=EXPECTED)],reason='합성 원장 정답'))
    policy=policy_engine.activate(w.owner,w.company,policy['id'],PolicyAction(expected_revision=policy['revision'],reason='격리된 시험만'))
    permit=policy_engine.evaluate(w.owner,w.company,policy['id'],invoice['id']);posted=policy_engine.execute(w.owner,w.company,permit['id'])
    raw=(CARD_HEADER+f'2026-09-01,{CARD},시험상점,1100,CARD-001,승인,KRW,{reference},{merchant},\n').encode()
    work=read(w,raw,'card-charge-source');card=next(e for e in work.list(w.owner,w.company) if e['kind']=='card')
    return CardBooks(w.svc,today=lambda:date(2026,9,30)),invoice,card,posted


def reviewed_post(w,engine,data):
    plan=engine.propose(w.owner,w.company,data)
    engine.approve(w.reviewer,w.company,plan['id'],CardBookReview(expected_revision=plan['revision'],reason='원매입·카드 식별·원금액 대조'))
    journal=engine.post(w.owner,w.company,plan['id'])
    return plan,journal


def charge(w):
    engine,invoice,card,posted=setup(w)
    p,j=reviewed_post(w,engine,CardBookInput(operation='invoice_charge',source_event_id=card['id'],invoice_event_id=invoice['id']))
    return engine,p,card,posted


def cancellation(w,engine,prior,amount=200,key='cancel-source-001'):
    raw=(CARD_HEADER+f'2026-09-02,{CARD},시험상점,-{amount},CANCEL-{key},취소,KRW,,1111111111,CARD-001\n').encode()
    work=read(w,raw,key);event=next(e for e in work.list(w.owner,w.company) if e['values']['reference']=='CANCEL-'+key)
    return reviewed_post(w,engine,CardBookInput(operation='cancellation',source_event_id=event['id'],prior_plan_id=prior['id']))


def bank(w,gross=1100,refund=False,key='card-bank-source'):
    raw=(BANK_HEADER+f'2026-09-03,카드대금,{gross if refund else 0},{0 if refund else gross},{BANK},{CARD},KRW,{key}\n').encode()
    work=read(w,raw,key);return next(e for e in work.list(w.owner,w.company) if e['kind']=='bank' and e['values']['reference']==key)


def account_net(w,account):
    with w.db.connect() as c:return sum(l['amount']*(1 if l['side']=='debit' else -1) for j in w.db.list(c,w.company,'journal') if j['state']=='POSTED' for l in j['lines'] if l['account']==account)


def test_invoice_card_bank_chain_has_one_expense_one_vat_and_one_cash_payment(w):
    engine,p,event,original=charge(w)
    assert account_net(w,'expense')==1000 and account_net(w,'vat_input')==100
    assert account_net(w,'payable')==0 and account_net(w,'card_payable')==-1100
    payment=bank(w)
    plan,posted=reviewed_post(w,engine,CardBookInput(operation='billing',source_event_id=payment['id'],statement_plan_ids=[p['id']]))
    assert account_net(w,'expense')==1000 and account_net(w,'vat_input')==100
    assert account_net(w,'cash')==-1100 and account_net(w,'card_payable')==0
    assert engine.post(w.owner,w.company,plan['id'])==posted
    with w.db.connect() as c:
        assert len(w.db.list(c,w.company,'journal'))==3
        assert w.db.get(c,w.company,'journal',original['id'])==original
        assert w.db.verify_audit(c,w.company)['valid']


def test_partial_cancellation_reduces_only_card_debt_not_tax_twice(w):
    engine,p,event,original=charge(w);cancel,_=cancellation(w,engine,p,200)
    payment=bank(w,900)
    reviewed_post(w,engine,CardBookInput(operation='billing',source_event_id=payment['id'],statement_plan_ids=[p['id'],cancel['id']]))
    assert account_net(w,'card_payable')==0 and account_net(w,'cash')==-900
    assert account_net(w,'expense')==1000 and account_net(w,'vat_input')==100
    assert account_net(w,'payable')==-200  # invoice correction is a separate source event


def test_cancel_after_payment_then_actual_bank_refund(w):
    engine,p,event,original=charge(w);payment=bank(w)
    reviewed_post(w,engine,CardBookInput(operation='billing',source_event_id=payment['id'],statement_plan_ids=[p['id']]))
    cancel,_=cancellation(w,engine,p,200)
    refund=bank(w,200,refund=True,key='card-bank-refund')
    reviewed_post(w,engine,CardBookInput(operation='billing',source_event_id=refund['id'],statement_plan_ids=[cancel['id']]))
    assert account_net(w,'cash')==-900 and account_net(w,'card_payable')==0
    assert engine.list(w.owner,w.company)['balances'][CARD]==0


@pytest.mark.parametrize('options',[{'reference':'OTHER-INVOICE'},{'merchant':'9999999999'}])
def test_card_link_requires_source_reference_and_counterparty(w,options):
    engine,invoice,card,_=setup(w,**options)
    with pytest.raises(DomainError) as error:engine.propose(w.owner,w.company,CardBookInput(operation='invoice_charge',source_event_id=card['id'],invoice_event_id=invoice['id']))
    assert error.value.code=='CARD_REFERENCE_REQUIRED'


def test_cancellation_total_cannot_exceed_original(w):
    engine,p,event,original=charge(w);cancellation(w,engine,p,1000)
    with pytest.raises(DomainError) as error:cancellation(w,engine,p,101,key='cancel-overrun')
    assert error.value.code=='CARD_CANCEL_OVERRUN'
    assert account_net(w,'card_payable')==-100


def test_bank_payment_cannot_be_matched_to_card_items_twice(w):
    engine,p,event,_=charge(w);payment=bank(w)
    reviewed_post(w,engine,CardBookInput(operation='billing',source_event_id=payment['id'],statement_plan_ids=[p['id']]))
    second=bank(w,key='different-bank-payment')
    with pytest.raises(DomainError) as error:engine.propose(w.owner,w.company,CardBookInput(operation='billing',source_event_id=second['id'],statement_plan_ids=[p['id']]))
    assert error.value.code=='CARD_BILL_ALREADY_USED'


def test_mismatched_card_bill_amount_is_not_written_off(w):
    engine,p,event,_=charge(w);payment=bank(w,1000)
    with pytest.raises(DomainError) as error:engine.propose(w.owner,w.company,CardBookInput(operation='billing',source_event_id=payment['id'],statement_plan_ids=[p['id']]))
    assert error.value.code=='CARD_BILL_TOTAL' and account_net(w,'cash')==0


def test_facts_reprocessing_or_reviewer_revocation_invalidate_approved_charge(w):
    engine,invoice,card,_=setup(w);p=engine.propose(w.owner,w.company,CardBookInput(operation='invoice_charge',source_event_id=card['id'],invoice_event_id=invoice['id']))
    engine.approve(w.reviewer,w.company,p['id'],CardBookReview(expected_revision=p['revision'],reason='독립 대조'))
    ext=Extractions(w.svc);doc=ext.uploads.document(w.owner,w.company,card['document_ids'][0])
    ext.reprocess(w.owner,w.company,doc['id'],ReprocessInput(expected_revision=doc['revision'],idempotency_key='card-reparse-test',reason='재추출'))
    with pytest.raises(DomainError):engine.post(w.owner,w.company,p['id'])
    assert account_net(w,'card_payable')==0


def test_card_no_self_approval_and_cross_company(w):
    engine,invoice,card,_=setup(w);p=engine.propose(w.owner,w.company,CardBookInput(operation='invoice_charge',source_event_id=card['id'],invoice_event_id=invoice['id']))
    with pytest.raises(DomainError):engine.approve(w.owner,w.company,p['id'],CardBookReview(expected_revision=p['revision'],reason='self'))
    with pytest.raises(DomainError):engine.post(w.owner,w.company,p['id'])
    with pytest.raises(DomainError):engine.list(w.other_owner,w.company)


def test_two_workers_cannot_multiply_card_charge(w):
    engine,invoice,card,_=setup(w);p=engine.propose(w.owner,w.company,CardBookInput(operation='invoice_charge',source_event_id=card['id'],invoice_event_id=invoice['id']))
    engine.approve(w.reviewer,w.company,p['id'],CardBookReview(expected_revision=p['revision'],reason='독립 대조'))
    with ThreadPoolExecutor(max_workers=2) as pool:r=list(pool.map(lambda _:engine.post(w.owner,w.company,p['id']),range(2)))
    assert r[0]['id']==r[1]['id'] and account_net(w,'card_payable')==-1100


def test_invoice_already_settled_by_card_cannot_be_paid_again_by_bank(w):
    from price.event_settlements import EventSettlements
    from price.payment_books import PaymentBooks,PaymentRequest
    engine,p,event,original=charge(w)
    raw=('거래일시,거래내용,입금액,출금액,계좌번호,상대방사업자등록번호,청구참조번호,거래번호,통화\n'
         f'2026-09-04,공급자 별도지급,0,1100,{BANK},1111111111,SYNTHETIC-01,BANK-DOUBLE,KRW\n').encode()
    work=read(w,raw,'bank-double-source');EventSettlements(w.svc).run_once(w.company)
    bank_event=next(e for e in work.list(w.owner,w.company) if e['kind']=='bank')
    with pytest.raises(DomainError) as error:PaymentBooks(w.svc).propose(w.owner,w.company,PaymentRequest(payment_event_id=bank_event['id']))
    assert error.value.code=='PAYMENT_INVOICE_OVERPAID'


def test_fact_questions_reuse_source_linked_invoice_purpose(w):
    engine,invoice,card,_=setup(w)
    result=next(e for e in Casework(w.svc).list(w.owner,w.company) if e['id']==card['id'])
    assert not result['questions']
    assert result['facts']['business_use']=='business' and result['facts']['use_type']=='operations'
