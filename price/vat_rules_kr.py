"""Narrow, source-attributed Korean VAT rules, reviewed facts in / proposal out.

Sources retrieved 2026-09-29:
- VAT Act article 39, current text effective 2026-01-02:
  https://www.law.go.kr/LSW/lsLawLinkInfo.do?chrClsCd=010202&lsId=001571&lsJoLnkSeq=900316725&print=print
- Enforcement Decree article 81, effective 2026-02-27:
  https://www.law.go.kr/LSW/lsLawLinkInfo.do?chrClsCd=010202&lsId=003666&lsJoLnkSeq=900328517&print=print
- NTS general taxpayer calculation overview:
  https://www.nts.go.kr/nts/cm/cntnts/cntntsView.do?cntntsId=7695&mi=2272

This evaluates explicit facts. No bank merchant name by itself proves deductibility.
It is not full statutory coverage or a Hometax submission format.
"""
from __future__ import annotations
from datetime import date
from decimal import Decimal,ROUND_DOWN
from typing import Annotated,Literal
from pydantic import StrictBool,StrictInt,Field
from .upload_models import UploadInput
from .db import DomainError

ART39='https://www.law.go.kr/LSW/lsLawLinkInfo.do?chrClsCd=010202&lsId=001571&lsJoLnkSeq=900316725&print=print'
ART81='https://www.law.go.kr/LSW/lsLawLinkInfo.do?chrClsCd=010202&lsId=003666&lsJoLnkSeq=900328517&print=print'
FLOW='https://www.nts.go.kr/nts/cm/cntnts/cntntsView.do?cntntsId=7695&mi=2272'
Money=Annotated[StrictInt,Field(ge=0,le=1_000_000_000_000)]


class InputTaxFacts(UploadInput):
    tax:Money
    supply_date:date
    business_related:StrictBool|None=None
    entertainment:StrictBool|None=None
    exempt_business:StrictBool|None=None
    land_related:StrictBool|None=None
    passenger_car:StrictBool|None=None
    vehicle_direct_business_exception:StrictBool|None=None
    invoice_valid:StrictBool|None=None
    registered_at_supply:StrictBool|None=None
    exception_reviewed:StrictBool=False
    exception_allows:StrictBool|None=None
    common_use:StrictBool|None=None


def assess_input_vat(facts:InputTaxFacts):
    f=InputTaxFacts.model_validate(facts)
    base={'rulepack':'kr-vat-input-2026.1','tax':f.tax,'source':ART39,'legal_applicability_reviewed':False,'accounting_posted':False}
    if not date(2026,1,2)<=f.supply_date<=date(2026,12,31):
        return {**base,'decision':'REVIEW','deductible_tax':None,'reason_codes':['RULE_DATE_OUTSIDE_VALIDATED_WINDOW']}
    reasons=[]
    if f.business_related is False:reasons.append('ARTICLE39_1_4_NONBUSINESS')
    if f.entertainment is True:reasons.append('ARTICLE39_1_6_ENTERTAINMENT')
    if f.exempt_business is True:reasons.append('ARTICLE39_1_7_EXEMPT_USE')
    if f.land_related is True:reasons.append('ARTICLE39_1_7_LAND_REVIEWED_CATEGORY')
    if f.passenger_car is True and f.vehicle_direct_business_exception is False:reasons.append('ARTICLE39_1_5_CAR')
    if reasons:return {**base,'decision':'NONDEDUCTIBLE','deductible_tax':0,'reason_codes':reasons}
    if f.invoice_valid is False or f.registered_at_supply is False:
        if not f.exception_reviewed or f.exception_allows is None:
            return {**base,'decision':'REVIEW','deductible_tax':None,'reason_codes':['DOCUMENT_OR_REGISTRATION_EXCEPTION_REVIEW']}
        if not f.exception_allows:return {**base,'decision':'NONDEDUCTIBLE','deductible_tax':0,'reason_codes':['REVIEWED_DOCUMENT_OR_REGISTRATION_EXCLUSION']}
    needed=[k for k in ('business_related','entertainment','exempt_business','land_related','passenger_car','invoice_valid','registered_at_supply','common_use') if getattr(f,k) is None]
    if f.passenger_car and f.vehicle_direct_business_exception is None:needed.append('vehicle_direct_business_exception')
    if needed:return {**base,'decision':'REVIEW','deductible_tax':None,'reason_codes':['MISSING_FACTS'],'missing_facts':needed}
    if f.common_use:return {**base,'decision':'APPORTIONMENT_REQUIRED','deductible_tax':None,'reason_codes':['COMMON_TAXABLE_EXEMPT_USE']}
    return {**base,'decision':'DEDUCTIBLE_CANDIDATE','deductible_tax':f.tax,'reason_codes':['EXPLICIT_FACTS_NO_IDENTIFIED_EXCLUSION']}


class CommonInput(UploadInput):
    period_end:date
    common_input_tax:Money
    total_supply:Money
    exempt_supply:Money
    basis:Literal['turnover']='turnover'
    special_business:StrictBool=False
    preliminary_nondeductible:Money=0


def apportion_common_input(data:CommonInput):
    p=CommonInput.model_validate(data)
    if not date(2026,2,27)<=p.period_end<=date(2026,12,31):raise DomainError('VAT_RULE_DATE','검증한 시행기간 밖의 안분 규칙입니다.')
    if p.special_business or p.total_supply==0 or p.exempt_supply==0 or p.exempt_supply>=p.total_supply:
        raise DomainError('VAT_APPORTIONMENT_ALTERNATE_BASIS','공급가액이 없는 사업·신축면적·특수업종의 대체 안분 기준을 별도 검토해야 합니다.')
    exempt_ratio=Decimal(p.exempt_supply)/Decimal(p.total_supply)
    relief=p.common_input_tax<50_000 or (p.exempt_supply*100<p.total_supply*5 and p.common_input_tax<5_000_000)
    exact=Decimal(0) if relief else Decimal(p.common_input_tax)*exempt_ratio
    denied=int(exact.to_integral_value(rounding=ROUND_DOWN))
    return {'rulepack':'kr-vat-common-2026.02.27','source':ART81,'method':'turnover','ratio':{'numerator':p.exempt_supply,'denominator':p.total_supply},
            'de_minimis_exception':relief,'nondeductible_exact':str(exact),'nondeductible':denied,'deductible':p.common_input_tax-denied,
            'settlement_delta':denied-p.preliminary_nondeductible,'rounding':'won_toward_zero_for_workpaper','official_form':False,
            'notice':'총공급가액·면세공급가액 정의와 확정신고 정산·대체기준 적용은 검토가 필요합니다.'}
