from datetime import date
import pytest
from price.vat_returns import VatReturns,VatBuild,VatAdjustmentInput,VatReview
from price.vat_rules_kr import InputTaxFacts,assess_input_vat,CommonInput,apportion_common_input,FLOW
from price.models import AuthorityInput,CoverageInput
from price.db import DomainError


def basic_facts(**changes):
    data=dict(tax=1000,supply_date=date(2026,9,1),business_related=True,entertainment=False,exempt_business=False,land_related=False,passenger_car=False,invoice_valid=True,registered_at_supply=True,common_use=False)
    data.update(changes);return InputTaxFacts(**data)


@pytest.mark.parametrize('changes,decision',[
 ({},'DEDUCTIBLE_CANDIDATE'),({'business_related':False},'NONDEDUCTIBLE'),({'entertainment':True},'NONDEDUCTIBLE'),
 ({'exempt_business':True},'NONDEDUCTIBLE'),({'land_related':True},'NONDEDUCTIBLE'),
 ({'passenger_car':True,'vehicle_direct_business_exception':False},'NONDEDUCTIBLE'),
 ({'passenger_car':True,'vehicle_direct_business_exception':None},'REVIEW'),
 ({'invoice_valid':False},'REVIEW'),({'registered_at_supply':False},'REVIEW'),
 ({'common_use':True},'APPORTIONMENT_REQUIRED'),({'business_related':None},'REVIEW'),
 ({'supply_date':date(2025,9,1)},'REVIEW')])
def test_tax_exclusions_and_unknown_facts_are_separate(changes,decision):
    result=assess_input_vat(basic_facts(**changes))
    assert result['decision']==decision and result['accounting_posted'] is False
    assert result['legal_applicability_reviewed'] is False


@pytest.mark.parametrize('tax,exempt,total,denied',[(49999,500,1000,0),(50000,500,1000,25000),(4999999,499,10000,0),(5000000,499,10000,249500),(100000,500,10000,5000)])
def test_common_input_boundaries_use_strict_less_than(tax,exempt,total,denied):
    r=apportion_common_input(CommonInput(period_end=date(2026,6,30),common_input_tax=tax,total_supply=total,exempt_supply=exempt))
    assert r['nondeductible']==denied and r['deductible']+r['nondeductible']==tax


def test_no_sales_or_one_business_absent_requires_alternate_basis():
    with pytest.raises(DomainError):apportion_common_input(CommonInput(period_end=date(2026,6,30),common_input_tax=100000,total_supply=0,exempt_supply=0))


def ready_period(w):
    w.post(w.tx(net=10000,tax=1000,gross=11000,external_id='sale-tax'))
    w.post(w.tx(direction='purchase',net=4000,tax=400,gross=4400,deductible=True,external_id='purchase-deductible'))
    w.post(w.tx(direction='purchase',net=1000,tax=100,gross=1100,deductible=False,external_id='purchase-nondeductible'))
    p=w.period();p=w.svc.coverage(w.reviewer,w.company,p['id'],CoverageInput(expected_revision=p['revision'],opening_reviewed=True,sources_confirmed=['manual'],evidence_note='합성 범위 대조'))
    return w.svc.period_action(w.reviewer,w.company,p['id'],'close',p['revision'],'원본·회계 검증')


def authority(w):
    a=w.svc.add_authority(w.owner,w.company,AuthorityInput(title='합성 검토용 국세청 계산흐름 근거',url=FLOW,effective_from=date(2026,1,1),effective_to=date(2026,12,31),excerpt='시험용 근거 참조, 실제 법률 자문 정답 인증 아님',kind='sample'))
    return w.svc.review_authority(w.reviewer,w.company,a['id'],a['revision'],'시험 근거 확인')


def test_return_automatically_maps_posted_journals_and_non_deductible(w):
    p=ready_period(w);engine=VatReturns(w.svc);r=engine.build(w.owner,w.company,VatBuild(period_id=p['id']))
    assert r['totals']['output_vat']==1000 and r['totals']['deductible_input_vat']==400
    assert r['totals']['tax_due_or_refund_before_payment_rounding']==600
    assert r['fields']['purchase_general']['tax']==500 and r['fields']['nondeductible']['tax']==100
    assert r['ledger_comparison']['agrees_before_adjustments']
    assert len(r['trace'])==3 and not r['blockers']
    assert engine.build(w.owner,w.company,VatBuild(period_id=p['id']))['id']==r['id']
    assert r['official_form'] is False and not r['live_submission_ready']
    reviewed=engine.review(w.reviewer,w.company,r['id'],VatReview(expected_revision=r['revision'],reason='합성 검토표 대조'))
    assert reviewed['state']=='REVIEWED_WORKPAPER'


def test_reviewed_adjustments_preserve_formula_and_source_evidence(w):
    p=ready_period(w);engine=VatReturns(w.svc);a=authority(w)
    for i,(kind,value) in enumerate([('bad_debt_output_adjustment',-100),('input_omitted',50),('other_deductible_input',20),('nondeductible_input',20),('reviewed_tax_credit',50),('prior_unrefunded',100),('pre_notified_tax',20),('penalty',30)]):
        r=engine.add_adjustment(w.owner,w.company,VatAdjustmentInput(period_id=p['id'],kind=kind,tax_amount=value,evidence_ids=[w.evidence],authority_ids=[a['id']],reason='합성 조정 사유',reference='adj-'+str(i)))
        engine.review_adjustment(w.reviewer,w.company,r['id'],VatReview(expected_revision=r['revision'],reason='금액·해당 조건 대조'))
    result=engine.build(w.owner,w.company,VatBuild(period_id=p['id']))
    assert result['totals']['tax_due_or_refund_before_payment_rounding']==310
    assert len(result['trace'])==11 and not result['blockers']


def test_unreviewed_adjustment_is_not_silently_counted(w):
    p=ready_period(w);engine=VatReturns(w.svc);a=authority(w)
    engine.add_adjustment(w.owner,w.company,VatAdjustmentInput(period_id=p['id'],kind='reviewed_tax_credit',tax_amount=500,evidence_ids=[w.evidence],authority_ids=[a['id']],reason='검토 대기',reference='unreviewed-1'))
    r=engine.build(w.owner,w.company,VatBuild(period_id=p['id']))
    assert r['totals']['applied_credit']==0
    assert any(x['code']=='VAT_ADJUSTMENT_UNREVIEWED' for x in r['blockers'])
    with pytest.raises(DomainError):engine.review(w.reviewer,w.company,r['id'],VatReview(expected_revision=r['revision'],reason='미완료 승인 거부'))


def test_old_workpaper_cannot_be_approved_after_late_source_or_adjustment(w):
    p=ready_period(w);engine=VatReturns(w.svc);r=engine.build(w.owner,w.company,VatBuild(period_id=p['id']))
    a=authority(w)
    with pytest.raises(DomainError) as e:engine.review(w.reviewer,w.company,r['id'],VatReview(expected_revision=r['revision'],reason='다시 확인'))
    assert e.value.code=='VAT_SOURCE_CHANGED'


def test_corporate_card_issue_credit_not_allowed(w):
    p=ready_period(w);a=authority(w)
    with pytest.raises(DomainError) as e:VatReturns(w.svc).add_adjustment(w.owner,w.company,VatAdjustmentInput(period_id=p['id'],kind='corporate_card_issue_credit',tax_amount=100,evidence_ids=[w.evidence],authority_ids=[a['id']],reason='법인 제외 시험',reference='bad-credit'))
    assert e.value.code=='CORPORATE_CREDIT_NOT_ALLOWED'


def test_self_approval_and_cross_company_reading_blocked(w):
    p=ready_period(w);engine=VatReturns(w.svc);r=engine.build(w.owner,w.company,VatBuild(period_id=p['id']))
    with pytest.raises(DomainError):engine.review(w.owner,w.company,r['id'],VatReview(expected_revision=r['revision'],reason='내 승인'))
    with pytest.raises(DomainError):engine.list(w.other_owner,w.company)


def test_exact_reversal_reduces_aggregates_without_original_mutation(w):
    original=w.post(w.tx(net=1000,tax=100,gross=1100))
    reversal=w.svc.reverse_journal(w.owner,w.company,original['id'],'2026-09-16','매출 취소',original['revision'])
    reversal=w.svc.transition_journal(w.owner,w.company,reversal['id'],'validate',reversal['revision'])
    reversal=w.svc.transition_journal(w.reviewer,w.company,reversal['id'],'approve',reversal['revision'],'취소 확인')
    w.svc.transition_journal(w.owner,w.company,reversal['id'],'post',reversal['revision'])
    p=w.period();r=VatReturns(w.svc).build(w.owner,w.company,VatBuild(period_id=p['id']))
    assert r['totals']['output_vat']==0 and r['fields']['sales_invoice']['supply']==0
    assert len(r['trace'])==2 and r['ledger_comparison']['agrees_before_adjustments']
