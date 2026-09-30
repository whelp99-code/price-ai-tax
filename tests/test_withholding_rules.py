from datetime import date
import pytest
from pydantic import ValidationError
from price.withholding_rules import WithholdingPayment,WorkDay,calculate_withholding
from price.db import DomainError


def daily(amounts,paid=date(2026,9,20)):
    return WithholdingPayment(category='resident_daily_wage',paid_on=paid,gross=sum(amounts),resident=True,income_classification_confirmed=True,daily_rows=[WorkDay(worked_on=date(2026,9,i+1),gross=amount) for i,amount in enumerate(amounts)])


def test_nts_daily_example_and_local_tax_match():
    r=calculate_withholding(daily([200000]*5))
    assert r['income_tax']==6750 and r['local_income_tax']==670
    assert r['net_to_recipient']==992580
    assert r['official_statement_submitted'] is False


def test_daily_999_is_exempt_only_if_entire_payment_below_1000():
    one=calculate_withholding(daily([187000]))
    two=calculate_withholding(daily([187000,187000]))
    assert one['income_tax']==0 and one['small_collection_not_collected']
    assert two['income_tax']==1990 and not two['small_collection_not_collected']
    assert two['local_income_tax']==190


def test_daily_deduction_not_a_negative_tax_or_refund():
    r=calculate_withholding(daily([50000,150000]))
    assert r['income_tax']==0 and r['net_to_recipient']==200000


def test_daily_excluded_compensation_is_traceable_not_arbitrary_hidden_input():
    r=calculate_withholding(WithholdingPayment(category='resident_daily_wage',paid_on=date(2026,9,2),gross=220000,resident=True,income_classification_confirmed=True,daily_rows=[WorkDay(worked_on=date(2026,9,1),gross=220000,excluded_non_taxable=20000)]))
    assert r['income_tax']==1350 and r['non_taxable']==20000
    assert r['trace'][0]['excluded_non_taxable']==20000


def test_small_personal_service_tax_is_still_withheld_after_july_2024():
    r=calculate_withholding(WithholdingPayment(category='resident_personal_service_business',paid_on=date(2026,9,1),gross=10000,resident=True,income_classification_confirmed=True))
    assert r['income_tax']==300 and r['local_income_tax']==30
    assert not r['small_collection_not_collected']
    assert r['net_to_recipient']==9670


def test_nts_business_example():
    r=calculate_withholding(WithholdingPayment(category='resident_personal_service_business',paid_on=date(2026,9,1),gross=5000000,resident=True,income_classification_confirmed=True))
    assert r['income_tax']==150000 and r['local_income_tax']==15000
    assert r['net_to_recipient']==4835000


@pytest.mark.parametrize('change',[{'resident':False},{'income_classification_confirmed':False},{'special_tax_relief':True},{'paid_on':date(2027,1,1)}])
def test_outside_profile_requires_real_review(change):
    values=dict(category='resident_personal_service_business',paid_on=date(2026,9,1),gross=100000,resident=True,income_classification_confirmed=True)
    values.update(change)
    with pytest.raises(DomainError):calculate_withholding(WithholdingPayment(**values))


def test_duplicate_workdays_cannot_claim_twice_daily_deduction():
    with pytest.raises(ValidationError):WithholdingPayment(category='resident_daily_wage',paid_on=date(2026,9,2),gross=300000,resident=True,income_classification_confirmed=True,daily_rows=[WorkDay(worked_on=date(2026,9,1),gross=150000),WorkDay(worked_on=date(2026,9,1),gross=150000)])


def test_gross_must_equal_payment_day_totals():
    with pytest.raises(ValidationError):WithholdingPayment(category='resident_daily_wage',paid_on=date(2026,9,2),gross=300000,resident=True,income_classification_confirmed=True,daily_rows=[WorkDay(worked_on=date(2026,9,1),gross=200000)])
