from datetime import date
from concurrent.futures import ThreadPoolExecutor
import pytest
from price.payment_books import PaymentBooks,PaymentRequest,PaymentReview
from price.autopolicies import PolicyEngine,PolicyInput,PolicyReview,PolicyTruthCase,PolicyAction
from price.event_settlements import EventSettlements
from price.models import AuthorityInput
from price.db import DomainError
from test_casework import setup_sources
from test_event_settlements import bank_rows
from test_source_ingestion import INVOICES


def prepared(w,amounts=(1100,)):
    work=setup_sources(w,number='1111111111',documents=[INVOICES,bank_rows(amounts)])
    events=work.list(w.owner,w.company);inv=next(x for x in events if x['kind']=='invoice')
    a=w.svc.add_authority(w.owner,w.company,AuthorityInput(title='합성 판매 전기 정책',url='https://www.law.go.kr',effective_from='2026-01-01',effective_to='2026-12-31',excerpt='시험용 원본 비교 근거',kind='sample'))
    a=w.svc.review_authority(w.reviewer,w.company,a['id'],a['revision'],'원본 확인')
    policy=PolicyEngine(w.svc,posting_enabled=True)
    p=policy.create(w.owner,w.company,PolicyInput(name='합성 고객 매출',direction='sale',counterparties=['2222222222'],use_type='operations',tax_category='standard',deductible=False,amount_limit=10000,effective_from='2026-01-01',effective_to='2026-12-31',authority_ids=[a['id']]))
    truth=[{'account':'receivable','side':'debit','amount':1100},{'account':'revenue','side':'credit','amount':1000},{'account':'vat_output','side':'credit','amount':100}]
    p=policy.review(w.reviewer,w.company,p['id'],PolicyReview(expected_revision=p['revision'],cases=[PolicyTruthCase(event_id=inv['id'],expected_lines=truth)],reason='기대 전표 대조'))
    p=policy.activate(w.owner,w.company,p['id'],PolicyAction(expected_revision=p['revision'],reason='격리 시험 실행'))
    allowed=policy.evaluate(w.owner,w.company,p['id'],inv['id']);journal=policy.execute(w.owner,w.company,allowed['id'])
    EventSettlements(w.svc).reconcile_strong_references(w.owner,w.company)
    banks=sorted((x for x in events if x['kind']=='bank'),key=lambda x:x['values']['reference'])
    return PaymentBooks(w.svc),banks,journal


def approved(w,engine,event):
    p=engine.propose(w.owner,w.company,PaymentRequest(payment_event_id=event['id']))
    return engine.approve(w.reviewer,w.company,p['id'],PaymentReview(expected_revision=p['revision'],reason='청구 참조와 입금 확인'))


def test_real_payment_journal_reduces_ar_not_revenue_twice(w):
    engine,banks,sale=prepared(w)
    plan=approved(w,engine,banks[0]);posted=engine.post(w.owner,w.company,plan['id'])
    assert posted['state']=='POSTED'
    assert posted['lines']==[{'account':'cash','side':'debit','amount':1100},{'account':'receivable','side':'credit','amount':1100}]
    assert engine.post(w.owner,w.company,plan['id'])['id']==posted['id']
    with w.db.connect() as c:
        rows=w.db.list(c,w.company,'journal');assert len(rows)==2
        net=lambda account:sum(l['amount']*(1 if l['side']=='debit' else -1) for j in rows for l in j['lines'] if l['account']==account)
        assert net('receivable')==0 and net('cash')==1100 and net('revenue')==-1000 and net('vat_output')==-100
        assert w.db.get(c,w.company,'journal',sale['id'])==sale
        assert w.db.verify_audit(c,w.company)['valid']


def test_partial_payments_post_independently_conserving_invoice(w):
    engine,banks,_=prepared(w,(400,300,400))
    for bank in banks:
        plan=approved(w,engine,bank);engine.post(w.owner,w.company,plan['id'])
    with w.db.connect() as c:
        rows=w.db.list(c,w.company,'journal')
        assert len(rows)==4
        assert sum(a['amount'] for a in w.db.list(c,w.company,'allocation'))==1100


def test_unreviewed_and_self_review_denied(w):
    engine,banks,_=prepared(w)
    p=engine.propose(w.owner,w.company,PaymentRequest(payment_event_id=banks[0]['id']))
    with pytest.raises(DomainError):engine.post(w.owner,w.company,p['id'])
    with pytest.raises(DomainError) as e:engine.approve(w.owner,w.company,p['id'],PaymentReview(expected_revision=p['revision'],reason='내 승인'))
    assert e.value.code=='SELF_APPROVAL'


def test_duplicate_concurrent_post_has_one_effect(w):
    engine,banks,_=prepared(w);plan=approved(w,engine,banks[0])
    with ThreadPoolExecutor(max_workers=2) as pool:r=list(pool.map(lambda _:engine.post(w.owner,w.company,plan['id']),range(2)))
    assert r[0]['id']==r[1]['id']
    with w.db.connect() as c:assert len(w.db.list(c,w.company,'journal'))==2


def test_bank_reextraction_invalidates_preapproved_payment(w):
    engine,banks,_=prepared(w);p=approved(w,engine,banks[0])
    from price.extractions import Extractions,ReprocessInput
    ex=Extractions(w.svc);doc=ex.uploads.document(w.owner,w.company,banks[0]['document_ids'][0])
    ex.reprocess(w.owner,w.company,doc['id'],ReprocessInput(expected_revision=doc['revision'],idempotency_key='bank-reextract-001',reason='원본 재확인'))
    with pytest.raises(DomainError):engine.post(w.owner,w.company,p['id'])


def test_revoked_reviewer_and_other_company_blocked(w):
    engine,banks,_=prepared(w);p=approved(w,engine,banks[0])
    with pytest.raises(DomainError):engine.list(w.other_owner,w.company)
    with w.db.transaction() as c:c.execute('DELETE FROM memberships WHERE user_id=? AND company_id=?',(w.reviewer.id,w.company))
    with pytest.raises(DomainError):engine.post(w.owner,w.company,p['id'])


def test_stale_second_partial_plan_needs_fresh_review(w):
    engine,banks,_=prepared(w,(400,700))
    a=approved(w,engine,banks[0]);b=approved(w,engine,banks[1])
    engine.post(w.owner,w.company,a['id'])
    with pytest.raises(DomainError) as error:engine.post(w.owner,w.company,b['id'])
    assert error.value.code=='PAYMENT_BASIS_CHANGED'
    renewed=approved(w,engine,banks[1]);engine.post(w.owner,w.company,renewed['id'])


def test_full_reconciliation_required_before_source_bookkeeping(w):
    work=setup_sources(w,number='1111111111',documents=[INVOICES,bank_rows([2200])])
    EventSettlements(w.svc).reconcile_strong_references(w.owner,w.company)
    bank=next(x for x in work.list(w.owner,w.company) if x['kind']=='bank')
    with pytest.raises(DomainError) as e:PaymentBooks(w.svc).propose(w.owner,w.company,PaymentRequest(payment_event_id=bank['id']))
    assert e.value.code=='PAYMENT_NOT_FULLY_MATCHED'
