"""2026 resident daily-worker / personal-service withholding calculations.

Official sources consulted 2026-09-29:
- NTS daily wages: https://www.nts.go.kr/nts/cm/cntnts/cntntsView.do?cntntsId=7863&mi=6427
- NTS small-collection exception (personal service after 2024-07-01):
  https://www.nts.go.kr/nts/cm/cntnts/cntntsView.do?cntntsId=7701&mi=2413
- NTS business withholding 3%:
  https://www.nts.go.kr/nts/cm/cntnts/cntntsView.do?cntntsId=7902&mi=6622
- Local Tax Act 103-13: https://www.law.go.kr/lsLinkCommonInfo.do?chrClsCd=010202&lsJoLnkSeq=1031061107
- Treasury Act 47: https://www.law.go.kr/LSW/lsLawLinkInfo.do?chrClsCd=010202&lsJoLnkSeq=900052683

Income classification must be independently established from contracts/work
facts. All rows describe one recipient's single actual payment, not an employer
aggregate or arbitrary splitting of daily wages into tax-free daily payments.
"""
from __future__ import annotations
from datetime import date
from decimal import Decimal,ROUND_DOWN
from typing import Annotated,Literal
from pydantic import Field,StrictBool,StrictInt,StrictStr,model_validator
from .upload_models import UploadInput
from .db import DomainError

Money=Annotated[StrictInt,Field(ge=0,le=1_000_000_000_000)]
SOURCES={
 'daily':'https://www.nts.go.kr/nts/cm/cntnts/cntntsView.do?cntntsId=7863&mi=6427',
 'threshold':'https://www.nts.go.kr/nts/cm/cntnts/cntntsView.do?cntntsId=7701&mi=2413',
 'business':'https://www.nts.go.kr/nts/cm/cntnts/cntntsView.do?cntntsId=7902&mi=6622',
 'local':'https://www.law.go.kr/lsLinkCommonInfo.do?chrClsCd=010202&lsJoLnkSeq=1031061107',
 'rounding':'https://www.law.go.kr/LSW/lsLawLinkInfo.do?chrClsCd=010202&lsJoLnkSeq=900052683'
}

class WorkDay(UploadInput):
    worked_on:date
    gross:Money
    excluded_non_taxable:Money=0
    @model_validator(mode='after')
    def valid_amount(self):
        if self.excluded_non_taxable>self.gross:raise ValueError('비과세 금액이 지급액을 초과합니다.')
        return self

class WithholdingPayment(UploadInput):
    category:Literal['resident_daily_wage','resident_personal_service_business']
    paid_on:date
    gross:Money
    resident:StrictBool
    income_classification_confirmed:StrictBool
    special_tax_relief:StrictBool=False
    daily_rows:Annotated[list[WorkDay],Field(max_length=366)]=[]
    @model_validator(mode='after')
    def validate_rows(self):
        if self.category=='resident_daily_wage':
            if not self.daily_rows or sum(r.gross for r in self.daily_rows)!=self.gross:raise ValueError('일별 지급액과 한 번에 지급한 총액이 일치해야 합니다.')
            days=[r.worked_on for r in self.daily_rows]
            if len(days)!=len(set(days)):raise ValueError('같은 근무일은 합산해 한 번만 기록해야 합니다.')
            if any(d>self.paid_on for d in days):raise ValueError('지급 후 미래 근무일이 포함되었습니다.')
        elif self.daily_rows:raise ValueError('사업소득에 일용근로 공제를 적용할 수 없습니다.')
        return self


def ten_won(value:Decimal|int)->int:
    number=Decimal(value)
    if number<0 or not number.is_finite():raise DomainError('WITHHOLDING_MONEY','원천징수 계산 금액이 올바르지 않습니다.')
    return int((number/10).to_integral_value(rounding=ROUND_DOWN))*10


def calculate_withholding(data:WithholdingPayment)->dict:
    p=WithholdingPayment.model_validate(data)
    if not p.resident or not p.income_classification_confirmed or p.special_tax_relief:
        raise DomainError('WITHHOLDING_CLASSIFICATION_REVIEW','거주자 구분·계약 실질·특례 여부를 독립 검토해야 합니다.')
    if not date(2026,1,2)<=p.paid_on<=date(2026,12,31):raise DomainError('WITHHOLDING_RULE_DATE','검증된 지급기간 밖의 규칙입니다.')
    traces=[];non_taxable=0
    if p.category=='resident_daily_wage':
        exact=Decimal(0)
        for row in sorted(p.daily_rows,key=lambda r:r.worked_on):
            if row.worked_on.year!=2026:raise DomainError('WITHHOLDING_WORK_DATE','전년도 귀속 일용소득의 적용 규칙을 따로 확인해야 합니다.')
            wages=row.gross-row.excluded_non_taxable
            base=max(wages-150000,0)
            before_credit=Decimal(base)*Decimal('0.06')
            credit=before_credit*Decimal('0.55');tax=before_credit-credit
            exact+=tax;non_taxable+=row.excluded_non_taxable
            traces.append({'worked_on':row.worked_on.isoformat(),'gross':row.gross,'excluded_non_taxable':row.excluded_non_taxable,
                           'daily_deduction':min(wages,150000),'base':base,'rate':'0.06','credit_rate':'0.55','tax_before_payment_rounding':str(tax)})
        small=exact<1000
        income=0 if small else ten_won(exact)
    else:
        exact=Decimal(p.gross)*Decimal('0.03')
        small=False  # personal-service withholding is excluded from the 1,000 KRW exemption
        income=ten_won(exact)
        traces.append({'gross':p.gross,'rate':'0.03','small_collection_exception':'personal_service_paid_after_2024-07-01'})
    local_exact=Decimal(income)*Decimal('0.1')
    local=ten_won(local_exact)
    return {'method':'kr-resident-single-payment-withholding/2026.09.29','category':p.category,'paid_on':p.paid_on.isoformat(),
            'gross':p.gross,'non_taxable':non_taxable,'income_tax_exact':str(exact),'income_tax':income,
            'small_collection_not_collected':small,'local_income_tax_exact':str(local_exact),'local_income_tax':local,
            'total_withholding':income+local,'net_to_recipient':p.gross-income-local,'trace':traces,
            'rounding_scope':'single_recipient_single_payment_not_arbitrary_daily_splits','rounding':'less_than_10_won_discarded',
            'sources':SOURCES,'official_statement_submitted':False,'social_insurance_calculated':False,
            'classification_verified_by_calculator':False}
