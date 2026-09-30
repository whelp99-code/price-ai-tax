from datetime import date
import pytest
from price.financial_statements import FinancialStatements
from price.db import DomainError
from test_opening_books import proposed
from price.opening_books import OpeningReview
from test_inventory_books import receipt,issue


def test_statements_balance_opening_period_and_accumulated_profit(w):
    openings,plan,_=proposed(w)
    openings.approve(w.reviewer,w.company,plan['id'],OpeningReview(expected_revision=plan['revision'],reason='기초 확인'))
    openings.post(w.owner,w.company,plan['id'])
    w.post(w.tx(net=1000,tax=100,gross=1100,event_date=date(2026,9,10)))
    r=FinancialStatements(w.svc).build(w.owner,w.company,date(2026,9,1),date(2026,9,30))
    assert r['balance_sheet']['assets']==12201
    assert r['balance_sheet']['liabilities']==1201
    assert r['balance_sheet']['equity_including_profit']==11000
    assert r['balance_sheet']['difference']==0 and r['trial_balance']['difference']==0
    assert r['income_statement']['net_profit']==1000
    assert r['cash']['opening']==10000 and r['cash']['closing']==10000 and r['cash']['reconciles']
    assert r['official_tax_return'] is False


def test_cash_without_opening_stays_unknown_not_zero(w):
    w.post(w.tx())
    r=FinancialStatements(w.svc).build(w.owner,w.company,date(2026,9,1),date(2026,9,30))
    assert r['cash']['opening'] is None and r['cash']['closing'] is None
    assert any(x['code']=='OPENING_BALANCE_NOT_VERIFIED' for x in r['blockers'])


def test_cogs_is_included_in_profit_and_balance_sheet(w):
    stock,lot,_=receipt(w,cost=1001)
    order,_=issue(w,stock,quantity=3);stock.post_issue(w.owner,w.company,order['id'])
    r=FinancialStatements(w.svc).build(w.owner,w.company,date(2026,9,1),date(2026,9,30))
    assert r['income_statement']['income']==1000 and r['income_statement']['expenses']==1001
    assert r['income_statement']['net_profit']==-1
    assert r['balance_sheet']['difference']==0


def test_statement_cross_company_and_invalid_period_denied(w):
    engine=FinancialStatements(w.svc)
    with pytest.raises(DomainError):engine.build(w.other_owner,w.company,date(2026,9,1),date(2026,9,30))
    with pytest.raises(DomainError):engine.build(w.owner,w.company,date(2026,9,30),date(2026,9,1))


def test_unposted_cost_schedule_keeps_financial_report_provisional(w):
    from test_book_schedules import approved
    service,plan,_=approved(w)
    r=FinancialStatements(w.svc).build(w.owner,w.company,date(2026,9,1),date(2026,9,30))
    assert any(x['code']=='SCHEDULE_NOT_POSTED' for x in r['blockers'])
    assert r['book_values_verified'] is False and r['period_closed'] is False
