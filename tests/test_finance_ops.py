from datetime import date
import pytest
from price.finance_ops import prorate_days,asset_schedule,fifo_cost,FinanceOps,ScheduleInput
from price.models import CoverageInput
from price.ingestion import Ingestion
from price.db import DomainError
from test_source_ingestion import upload,BANK


def test_day_allocation_preserves_odd_krw_and_leap_year():
    result=prorate_days(10001,date(2024,1,31),date(2024,3,1))
    assert result['days']==31 and [p['days'] for p in result['periods']]==[1,29,1]
    assert sum(p['amount'] for p in result['periods'])==10001 and result['conserved']
    assert all(type(p['amount']) is int for p in result['periods'])


def test_straight_line_cost_and_salvage_across_year():
    result=asset_schedule(10001,1000,3,date(2026,11,15))
    assert [x['month'] for x in result['periods']]==['2026-11','2026-12','2027-01']
    assert sum(x['expense'] for x in result['periods'])==9001
    assert result['periods'][-1]['closing_book_value']==1000
    assert result['tax_depreciation_verified'] is False


@pytest.mark.parametrize('total',[True,-1,1.0,'1'])
def test_non_integer_schedule_cost_rejected(total):
    with pytest.raises(ValueError):prorate_days(total,date(2026,1,1),date(2026,1,2))


def test_period_limits_and_nonexistent_inventory():
    with pytest.raises(ValueError):prorate_days(1,date(2026,2,1),date(2026,1,1))
    with pytest.raises(ValueError):asset_schedule(100,101,12,date(2026,1,1))
    with pytest.raises(ValueError):fifo_cost([{'id':'lot1','quantity':2,'total_cost':3}],3)


def test_fifo_conserves_rounding_across_multiple_consumptions():
    lots=[{'id':'a','quantity':3,'total_cost':100},{'id':'b','quantity':2,'total_cost':81}]
    first=fifo_cost(lots,2);second=fifo_cost(first['remaining_lots'],2);last=fifo_cost(second['remaining_lots'],1)
    assert first['cost_of_sales']+second['cost_of_sales']+last['cost_of_sales']==181
    assert not last['remaining_lots']
    assert all(x['cost_conserved'] for x in (first,second,last))
    assert lots[0]['quantity']==3


def test_missing_upload_cannot_be_hidden_by_manual_source_attestation(w):
    p=w.period();p=w.svc.coverage(w.reviewer,w.company,p['id'],CoverageInput(expected_revision=p['revision'],opening_reviewed=True,sources_confirmed=['manual'],evidence_note='synthetic old coverage'))
    upload(w,BANK)
    ready=w.svc.readiness(w.owner,w.company,p['id'])
    assert not ready['ready'] and any(x['code']=='UNRESOLVED_SOURCE_DOCUMENT' for x in ready['blockers'])
    Ingestion(w.svc).run_once(w.company)
    ready=w.svc.readiness(w.owner,w.company,p['id'])
    assert any(x['code']=='UNLINKED_SOURCE_RECORD' for x in ready['blockers'])
    with pytest.raises(DomainError):w.svc.period_action(w.reviewer,w.company,p['id'],'close',p['revision'],'must remain open')


def test_cash_receipt_not_same_as_accrued_revenue(w):
    w.post(w.tx())
    report=FinanceOps(w.svc).report(w.owner,w.company,date(2026,9,1),date(2026,9,30))
    assert report['profit']['revenue']==1000 and report['cash']['received']==0
    assert report['cash']['closing_balance'] is None and not report['company_data_complete']
    assert report['account_totals']['revenue']['journal_ids']


def test_schedule_proposal_requires_source_and_never_posts(w):
    ops=FinanceOps(w.svc)
    data=ScheduleInput(kind='accrual',label='연간 구독',cost=120000,start='2026-01-01',end='2026-12-31',evidence_ids=[w.evidence],idempotency_key='accrual-source-01')
    schedule=ops.propose_schedule(w.reviewer,w.company,data)
    assert schedule['result']['conserved'] and schedule['posting_performed'] is False
    assert ops.propose_schedule(w.reviewer,w.company,data)['id']==schedule['id']
    with pytest.raises(DomainError):ops.propose_schedule(w.other_owner,w.company,data)
    with w.db.connect() as c:assert not w.db.list(c,w.company,'journal')
