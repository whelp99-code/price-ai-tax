from datetime import date
import pytest
from price.opening_books import OpeningBooks,OpeningRequest,OpeningReview
from price.opening_sources import try_parse_opening
from price.ingestion import Ingestion
from price.db import DomainError
from price.models import CalculationInput,PeriodInput
from test_source_ingestion import upload

OPENING=('합성 전기말 잔액 자료\n기준일: 2026-09-01\n통화: KRW\n'
         '계정과목,차변잔액,대변잔액\n보통예금,10000,0\n상품,1001,0\n부가세대급금,100,0\n매입채무,0,1101\n자본금,0,10000\n합계,11101,11101\n').encode()


def proposed(w,raw=OPENING):
    b=upload(w,raw,filename='전기말잔액.csv');reader=Ingestion(w.svc);result=reader.run_once(w.company)
    assert result['state']=='SUCCEEDED',result
    doc=b['files'][0]['document']['id'];engine=OpeningBooks(w.svc)
    return engine,engine.propose(w.owner,w.company,OpeningRequest(document_id=doc,idempotency_key='opening-test-0001')),doc


def test_original_opening_csv_is_auto_read_without_owner_mapping():
    result=try_parse_opening(OPENING)
    assert result['state']=='NORMALIZED' and result['record_count']==5
    assert result['records'][0]['values']['ledger_account']=='cash'
    assert result['records'][0]['fields']['debit']['row']==5
    assert result['document_kind']=='opening_balances'


def test_unknown_account_unbalanced_and_missing_date_not_guessed():
    for raw in [OPENING.replace('보통예금'.encode(),'알수없는계정'.encode()),OPENING.replace(b'10000,0',b'10001,0'),OPENING.replace('기준일: 2026-09-01\n'.encode(),b'')]:
        result=try_parse_opening(raw)
        assert result['state']=='VALIDATION_REQUIRED' and result['issues']


def test_reviewed_opening_posts_once_and_vat_opening_not_counted_as_current_input_tax(w):
    engine,plan,doc=proposed(w)
    with pytest.raises(DomainError):engine.post(w.owner,w.company,plan['id'])
    with pytest.raises(DomainError):engine.approve(w.owner,w.company,plan['id'],OpeningReview(expected_revision=plan['revision'],reason='자기 확인'))
    plan=engine.approve(w.reviewer,w.company,plan['id'],OpeningReview(expected_revision=plan['revision'],reason='전기말 잔액 정답 대조'))
    posted=engine.post(w.owner,w.company,plan['id'])
    assert posted['state']=='POSTED' and posted['opening_batch_id']==plan['id']
    assert engine.post(w.owner,w.company,plan['id'])['id']==posted['id']
    p=w.svc.create_period(w.owner,w.company,PeriodInput(label='기초 인수',start=date(2026,9,1),end=date(2026,9,30)))
    calc=w.svc.calculate(w.owner,w.company,CalculationInput(period_id=p['id']))
    assert calc['totals']['input_vat']==0
    assert w.svc.trial_balance(w.owner,w.company)['balanced']
    with w.db.connect() as c:assert w.db.verify_audit(c,w.company)['valid']


def test_second_opening_cannot_duplicate_balances(w):
    engine,first,_=proposed(w)
    engine.approve(w.reviewer,w.company,first['id'],OpeningReview(expected_revision=first['revision'],reason='잔액 확인'))
    engine.post(w.owner,w.company,first['id'])
    with pytest.raises(DomainError) as error:
        engine.propose(w.owner,w.company,OpeningRequest(document_id=first['document_id'],idempotency_key='opening-test-0002'))
    assert error.value.code=='OPENING_ALREADY_POSTED'


def test_source_reprocessing_invalidates_opening_approval(w):
    engine,plan,doc=proposed(w)
    engine.approve(w.reviewer,w.company,plan['id'],OpeningReview(expected_revision=plan['revision'],reason='잔액 확인'))
    from price.extractions import Extractions,ReprocessInput
    extractions=Extractions(w.svc);current=extractions.uploads.document(w.owner,w.company,doc)
    extractions.reprocess(w.owner,w.company,doc,ReprocessInput(expected_revision=current['revision'],idempotency_key='opening-source-reprocess',reason='원본 재확인'))
    with pytest.raises(DomainError) as e:engine.post(w.owner,w.company,plan['id'])
    assert e.value.code=='OPENING_SOURCE_CHANGED'


def test_opening_cannot_be_posted_later_than_existing_history(w):
    w.post(w.tx(event_date=date(2026,8,31)))
    engine,plan,_=proposed(w)
    engine.approve(w.reviewer,w.company,plan['id'],OpeningReview(expected_revision=plan['revision'],reason='잔액 확인'))
    with pytest.raises(DomainError) as e:engine.post(w.owner,w.company,plan['id'])
    assert e.value.code=='OPENING_DATE_CONFLICT'


def test_posted_opening_prevents_backdated_entries(w):
    engine,plan,_=proposed(w)
    engine.approve(w.reviewer,w.company,plan['id'],OpeningReview(expected_revision=plan['revision'],reason='잔액 확인'))
    engine.post(w.owner,w.company,plan['id'])
    with pytest.raises(DomainError) as e:w.tx(event_date=date(2026,8,31))
    assert e.value.code=='BEFORE_OPENING_DATE'


def test_owner_financial_report_gets_verified_cash_opening(w):
    engine,plan,_=proposed(w)
    engine.approve(w.reviewer,w.company,plan['id'],OpeningReview(expected_revision=plan['revision'],reason='잔액 확인'))
    engine.post(w.owner,w.company,plan['id'])
    from price.finance_ops import FinanceOps
    report=FinanceOps(w.svc).report(w.owner,w.company,date(2026,9,1),date(2026,9,30))
    assert report['cash']['opening_balance']==10000 and report['cash']['closing_balance']==10000
    assert report['cash']['received']==report['cash']['paid']==0
    assert report['cash']['opening_balance_verified'] is True


def test_opening_reversal_is_not_current_tax_activity(w):
    engine,plan,_=proposed(w)
    engine.approve(w.reviewer,w.company,plan['id'],OpeningReview(expected_revision=plan['revision'],reason='잔액 확인'))
    original=engine.post(w.owner,w.company,plan['id'])
    rev=w.svc.reverse_journal(w.owner,w.company,original['id'],'2026-09-02','기초잔액 정정',original['revision'])
    rev=w.svc.transition_journal(w.owner,w.company,rev['id'],'validate',rev['revision'])
    rev=w.svc.transition_journal(w.reviewer,w.company,rev['id'],'approve',rev['revision'],'정정 검토')
    rev=w.svc.transition_journal(w.owner,w.company,rev['id'],'post',rev['revision'])
    assert rev['current_period_activity'] is False
    p=w.period();calc=w.svc.calculate(w.owner,w.company,CalculationInput(period_id=p['id']))
    assert calc['totals']['input_vat']==0


def test_worker_creates_opening_proposal_only_and_idempotently(w):
    batch=upload(w,OPENING,filename='기초자료.csv');reader=Ingestion(w.svc);reader.run_once(w.company)
    engine=OpeningBooks(w.svc)
    assert engine.run_once(w.company)['state']=='PROPOSED'
    assert engine.run_once(w.company)['processed'] is False
    with w.db.connect() as c:
        assert len(w.db.list(c,w.company,'opening_batch'))==1
        assert not w.db.list(c,w.company,'journal')
