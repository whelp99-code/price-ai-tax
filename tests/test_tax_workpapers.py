from datetime import date
from decimal import Decimal
import pytest
from price.tax_workpapers import progressive_component,TaxWorkpapers,CorporateWorkpaperInput,CarryforwardInput,PayrollReconciliationInput
from price.models import AuthorityInput
from price.db import DomainError


@pytest.mark.parametrize('base,category,expected',[
    (0,'general_for_profit','0'),(200000000,'general_for_profit','20000000'),
    (200000001,'general_for_profit','20000000.20'),(20000000000,'general_for_profit','3980000000'),
    (300000000000,'general_for_profit','65580000000'),(300000000001,'general_for_profit','65580000000.25'),
    (200000000,'special_small_corporation','40000000'),(20000000000,'special_small_corporation','4000000000'),
    (300000000000,'special_small_corporation','65600000000'),
    (2000000000,'cooperative_special_article72','180000000'),(2000000010,'cooperative_special_article72','180000001.50')
])
def test_2026_nts_rate_table_boundaries(base,category,expected):
    value=progressive_component(base,category,date(2026,1,1),date(2026,12,31))
    assert Decimal(value['raw_rate_component'])==Decimal(expected)
    assert value['source']['publisher']=='국세청'
    assert value['final_tax_payable'] is None and not value['statutory_return_complete']


@pytest.mark.parametrize('start,end',[(date(2025,1,1),date(2025,12,31)),(date(2026,1,1),date(2026,6,30)),(date(2027,1,1),date(2027,12,31))])
def test_rate_component_does_not_apply_outside_verified_year_scope(start,end):
    with pytest.raises(ValueError):progressive_component(1000000,'general_for_profit',start,end)


def test_category_not_guessed_from_company_size():
    with pytest.raises(ValueError):progressive_component(1000000,'automatic',date(2026,1,1),date(2026,12,31))


def test_corporate_workpaper_reconciles_adjustments_without_claiming_complete_tax(w):
    authority=w.svc.add_authority(w.owner,w.company,AuthorityInput(title='Synthetic adjustment fixture',url='https://www.law.go.kr',effective_from='2026-01-01',effective_to='2026-12-31',excerpt='Not an actual tax rule.',kind='sample'))
    authority=w.svc.review_authority(w.reviewer,w.company,authority['id'],authority['revision'],'fixture review')
    data=CorporateWorkpaperInput(year_start='2026-01-01',year_end='2026-12-31',category='general_for_profit',book_profit=200000000,book_evidence_ids=[w.evidence],adjustments=[{'adjustment_id':'A1','label':'sample add-back','amount':10000000,'evidence_ids':[w.evidence],'authority_id':authority['id']}],approved_loss_use=5000000,loss_evidence_ids=[w.evidence],idempotency_key='corporate-paper-01')
    paper=TaxWorkpapers(w.svc).corporate(w.reviewer,w.company,data)
    assert paper['result']['taxable_base_candidate']==205000000
    assert Decimal(paper['result']['rate_component']['raw_rate_component'])==21000000
    assert not paper['statutory_return_complete'] and not paper['submitted']
    with pytest.raises(DomainError):TaxWorkpapers(w.svc).corporate(w.owner,w.company,data)


def test_carryforward_conservation_and_overuse(w):
    service=TaxWorkpapers(w.svc)
    data=CarryforwardInput(label='검토용 이월',opening=1000,additions=500,used=800,expired=100,period_end='2026-12-31',evidence_ids=[w.evidence],idempotency_key='carryforward-01')
    assert service.carryforward(w.reviewer,w.company,data)['result']['closing']==600
    with pytest.raises(DomainError):service.carryforward(w.reviewer,w.company,data.model_copy(update={'used':1500,'idempotency_key':'carryforward-02'}))


def test_payroll_amounts_only_reconcile_when_net_matches(w):
    base={'worker_reference':'SYNTHETIC-EMPLOYEE-01','accrual_month':'2026-09','payment_date':'2026-10-05','earnings':[{'code':'salary','amount':3000000,'evidence_id':w.evidence}],'deductions':[{'code':'reviewed-withholding','amount':200000,'evidence_id':w.evidence}],'expected_net_payment':2800000,'idempotency_key':'payroll-paper-01'}
    data=PayrollReconciliationInput(**base)
    r=TaxWorkpapers(w.svc).payroll(w.reviewer,w.company,data)
    assert r['result']['net_payment']==2800000 and r['result']['withholding_calculated'] is False
    with pytest.raises(DomainError):TaxWorkpapers(w.svc).payroll(w.reviewer,w.company,PayrollReconciliationInput(**{**base,'expected_net_payment':2700000,'idempotency_key':'payroll-paper-02'}))
    with pytest.raises(DomainError):TaxWorkpapers(w.svc).payroll(w.viewer,w.company,data)
