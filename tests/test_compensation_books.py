from datetime import date
from concurrent.futures import ThreadPoolExecutor
import pytest
from price.compensation_books import CompensationBooks,CompensationInput,CompensationReview
from price.withholding_rules import WithholdingPayment
from price.models import AuthorityInput
from price.db import DomainError
from test_casework import setup_sources


def fixture(w,net=96700):
    raw=('계좌번호: 123456789012\n통화: KRW\n거래일시,거래내용,입금액,출금액,거래번호\n'+f'2026-09-20 12:00:00,시험용 지급대상,0,{net},PAY-ONE\n').encode()
    work=setup_sources(w,documents=[raw]);bank=work.list(w.owner,w.company)[0]
    engine=CompensationBooks(w.svc,today=lambda:date(2026,12,31))
    data=CompensationInput(recipient_reference='SYNTHETIC-PERSON-01',bank_event_id=bank['id'],payment=WithholdingPayment(category='resident_personal_service_business',paid_on=date(2026,9,20),gross=100000,resident=True,income_classification_confirmed=True),evidence_ids=[w.evidence],reference='PAYROLL-ONE')
    a=w.svc.add_authority(w.owner,w.company,AuthorityInput(title='합성 인적용역 소득구분 검토',url='https://www.nts.go.kr',effective_from='2026-01-01',effective_to='2026-12-31',excerpt='실제 계약·소득구분은 독립 검토 필요, 이 자료는 시험용',kind='sample'))
    a=w.svc.review_authority(w.reviewer,w.company,a['id'],a['revision'],'시험용 근거 검토')
    return engine,data,a


def review(w,engine,p,a,**changes):
    body={'expected_revision':p['revision'],'reason':'수취인·계약·귀속·기존 비용 부재 확인','authority_ids':[a['id']],'prior_expense_absent_verified':True,'recognition_period_verified':True,'recipient_matches_bank_verified':True}
    body.update(changes)
    return engine.review(w.reviewer,w.company,p['id'],CompensationReview(**body))


def test_withholding_really_posts_gross_net_national_and_local_separately(w):
    engine,data,a=fixture(w);p=engine.propose(w.owner,w.company,data);p=review(w,engine,p,a)
    j=engine.post(w.owner,w.company,p['id'])
    assert j['state']=='POSTED' and j['withholding_breakdown']=={'national':3000,'local':300}
    assert {x['account']:x['amount'] for x in j['lines']}=={'expense':100000,'cash':96700,'withholding_payable':3000,'local_withholding_payable':300}
    assert engine.post(w.owner,w.company,p['id'])['id']==j['id']
    statement=engine.monthly_statement(w.owner,w.company,2026,9)
    assert statement['national']==3000 and statement['local']==300 and statement['gross']==100000
    assert not statement['official_filing_format'] and not statement['tax_paid']
    with w.db.connect() as c:assert w.db.verify_audit(c,w.company)['valid']


def test_bank_mismatch_cannot_be_repaired_by_changing_computed_tax(w):
    engine,data,a=fixture(w,net=97000)
    with pytest.raises(DomainError) as err:engine.propose(w.owner,w.company,data)
    assert err.value.code=='PAYROLL_NET_MISMATCH'


def test_no_self_review_and_no_unreviewed_post(w):
    engine,data,a=fixture(w);p=engine.propose(w.owner,w.company,data)
    with pytest.raises(DomainError):engine.post(w.owner,w.company,p['id'])
    with pytest.raises(DomainError) as e:engine.review(w.owner,w.company,p['id'],CompensationReview(expected_revision=p['revision'],reason='자기확인',authority_ids=[a['id']],prior_expense_absent_verified=True,recognition_period_verified=True,recipient_matches_bank_verified=True))
    assert e.value.code=='SELF_APPROVAL'


def test_duplicate_bank_cannot_create_second_expense(w):
    engine,data,a=fixture(w);p=engine.propose(w.owner,w.company,data);review(w,engine,p,a);engine.post(w.owner,w.company,p['id'])
    with pytest.raises(DomainError) as e:engine.propose(w.owner,w.company,CompensationInput(**{**data.model_dump(),'reference':'DIFFERENT-REFERENCE'}))
    assert e.value.code=='PAYROLL_PAYMENT_DUPLICATE'


def test_same_payment_posting_concurrently_is_once(w):
    engine,data,a=fixture(w);p=engine.propose(w.owner,w.company,data);review(w,engine,p,a)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:engine.post(w.owner,w.company,p['id']),range(2)))
    assert results[0]['id']==results[1]['id']
    with w.db.connect() as c:assert len(w.db.list(c,w.company,'journal'))==1


def test_review_requires_expense_period_and_recipient_checks(w):
    engine,data,a=fixture(w);p=engine.propose(w.owner,w.company,data)
    with pytest.raises(DomainError):review(w,engine,p,a,prior_expense_absent_verified=False)
    with pytest.raises(DomainError):review(w,engine,p,a,recognition_period_verified=False)
    with pytest.raises(DomainError):review(w,engine,p,a,recipient_matches_bank_verified=False)


def test_changed_authority_source_or_review_is_detected_at_execution(w):
    engine,data,a=fixture(w);p=engine.propose(w.owner,w.company,data);p=review(w,engine,p,a)
    with w.db.transaction() as c:
        rule=w.db.get(c,w.company,'authority',a['id']);w.db.replace(c,w.company,'authority',a['id'],{**rule,'reviewed':False},rule['revision'])
    with pytest.raises(DomainError):engine.post(w.owner,w.company,p['id'])


def test_reading_or_posting_another_company_is_denied(w):
    engine,data,a=fixture(w);p=engine.propose(w.owner,w.company,data);review(w,engine,p,a)
    with pytest.raises(DomainError):engine.post(w.other_owner,w.company,p['id'])
    with pytest.raises(DomainError):engine.monthly_statement(w.other_owner,w.company,2026,9)
