from datetime import date
import pytest
from price.source_availability import SourceAvailability,range_present
from price.owner import OwnerView,CompanyFactsInput
from price.ingestion import Ingestion
from price.db import DomainError
from test_source_ingestion import upload,BANK
from test_source_periods import HEADER


def setup(w,raw=None):
    reader=Ingestion(w.svc)
    OwnerView(w.svc,reader.uploads).confirm(w.owner,w.company,CompanyFactsInput(expected_revision=0,name='합성 회사',mode='company',confirmed=True,instruments=[{'kind':'bank','identifier':'123456789012','currency':'KRW'}]))
    if raw is not None:upload(w,raw);reader.run_once(w.company)
    return SourceAvailability(w.svc)


def report(engine,w):return engine.report(w.owner,w.company,date(2026,9,1),date(2026,9,30))


def test_missing_file_generates_factual_source_request_not_zero_balance(w):
    e=setup(w);r=report(e,w);scope=r['instruments'][0]
    assert scope['status']=='NO_FILES'
    assert '원본을 올려주세요' in scope['owner_request']
    assert not r['completeness_verified'] and not r['financial_work_complete']


def test_observed_dates_do_not_claim_month_fully_supplied(w):
    e=setup(w,BANK);r=report(e,w);scope=r['instruments'][0]
    assert scope['status']=='QUERY_RANGE_NOT_ESTABLISHED'
    assert scope['sources'][0]['row_count']==2
    assert not scope['printed_range_covers_request']


def test_printed_range_distinct_from_independent_or_institution_acceptance(w):
    raw=BANK.replace('통화: KRW\n'.encode(),'통화: KRW\n조회기간: 2026-09-01 ~ 2026-09-30\n'.encode())
    e=setup(w,raw);r=report(e,w);scope=r['instruments'][0]
    assert scope['status']=='PRINTED_RANGE_PRESENT' and scope['printed_range_covers_request']
    assert not scope['completeness_verified'] and not r['period_closed']


def test_explicit_zero_activity_is_shown_without_financial_completion(w):
    e=setup(w,(HEADER+'거래내역이 없습니다\n').encode())
    scope=report(e,w)['instruments'][0]
    assert scope['sources'][0]['zero_activity_reported']
    assert scope['sources'][0]['row_count']==0
    assert scope['printed_range_covers_request'] and not scope['completeness_verified']


def test_request_inside_observed_span_does_not_lose_source(w):
    raw=BANK.replace(b'2026-09-02',b'2026-09-30')
    e=setup(w,raw);r=e.report(w.owner,w.company,date(2026,9,10),date(2026,9,20))
    assert len(r['instruments'][0]['sources'])==1
    assert not r['instruments'][0]['printed_range_covers_request']


def test_read_only_report_does_not_mutate_audit_or_periods(w):
    e=setup(w,BANK)
    with w.db.connect() as c:before=(c.execute('SELECT count(*) FROM audit').fetchone()[0],c.execute('SELECT count(*) FROM objects').fetchone()[0])
    report(e,w)
    with w.db.connect() as c:assert before==(c.execute('SELECT count(*) FROM audit').fetchone()[0],c.execute('SELECT count(*) FROM objects').fetchone()[0])
    with pytest.raises(DomainError):e.report(w.other_owner,w.company,date(2026,9,1),date(2026,9,30))


def test_interval_gaps_are_not_filled_by_dates_or_assumptions():
    assert not range_present([{'start':'2026-09-01','end':'2026-09-14'},{'start':'2026-09-16','end':'2026-09-30'}],date(2026,9,1),date(2026,9,30))
    assert range_present([{'start':'2026-09-01','end':'2026-09-15'},{'start':'2026-09-16','end':'2026-09-30'}],date(2026,9,1),date(2026,9,30))
