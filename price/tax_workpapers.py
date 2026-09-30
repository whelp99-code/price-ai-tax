"""Source-backed tax-rate components and evidence-linked year workpapers.

These are components, not a complete statutory return or professional tax
opinion. No home page can label them as filed, payable, or legally validated.
"""
from __future__ import annotations
from datetime import date
from decimal import Decimal,ROUND_DOWN
from typing import Annotated,Literal
from pydantic import Field,StrictInt,StrictStr,model_validator
from .db import DomainError,digest
from .upload_models import UploadInput
from .auth import Actor

SOURCE_CORPORATE_2026='https://www.nts.go.kr/nts/cm/cntnts/cntntsView.do?cntntsId=7746&mi=2372'
RATE_SOURCE={'publisher':'국세청','title':'법인세 세율 (2026년 이후)','url':SOURCE_CORPORATE_2026,
             'retrieved_on':'2026-09-28','applies_to':'사업연도 개시일 2026-01-01 이후 안내표',
             'implementation_scope':'2026년 개시·12개월 사업연도 세율 구성요소만','expert_validated':False}
RATE_TABLES={
    'general_for_profit':[(200_000_000,'0.10'),(20_000_000_000,'0.20'),(300_000_000_000,'0.22'),(None,'0.25')],
    'nonprofit_taxable_business':[(200_000_000,'0.10'),(20_000_000_000,'0.20'),(300_000_000_000,'0.22'),(None,'0.25')],
    'special_small_corporation':[(20_000_000_000,'0.20'),(300_000_000_000,'0.22'),(None,'0.25')],
    'cooperative_special_article72':[(2_000_000_000,'0.09'),(None,'0.15')]
}


def progressive_component(taxable_base:int,category:str,year_start:date,year_end:date):
    if type(taxable_base) is not int or not 0<=taxable_base<=10**16:raise ValueError('Taxable base must be nonnegative integer KRW')
    if category not in RATE_TABLES:raise ValueError('Explicit reviewed taxpayer category required')
    if type(year_start) is not date or type(year_end) is not date or year_start.year!=2026 or (year_end-year_start).days not in (364,365):
        raise ValueError('Only 2026-starting full 12-month periods are supported by this rate component')
    # Reject arbitrary same-day-count ranges that are not the matching anniversary.
    try:anniversary=year_start.replace(year=year_start.year+1)
    except ValueError:raise ValueError('Period anniversary requires review')
    if (anniversary-year_end).days!=1:raise ValueError('Full-year anniversary mismatch')
    amount=Decimal(0);lower=0;trace=[]
    for upper,rate in RATE_TABLES[category]:
        portion=max(0,min(taxable_base,upper if upper is not None else taxable_base)-lower)
        component=Decimal(portion)*Decimal(rate)
        trace.append({'lower':lower,'upper':upper,'taxable_portion':portion,'rate':rate,'component':format(component,'f')})
        amount+=component
        if upper is None or taxable_base<=upper:break
        lower=upper
    return {'taxable_base':taxable_base,'category':category,'raw_rate_component':format(amount,'f'),'tranches':trace,
            'source':RATE_SOURCE,'legal_category_inferred':False,'final_tax_payable':None,'statutory_return_complete':False,
            'exclusions':['법인 분류 요건의 법적 판정','세무조정 정확성','결손·공제 요건과 한도','최저한세','추가세액','기납부·가산세','지방세','법정 신고서와 납부 단위']}


SignedMoney=Annotated[StrictInt,Field(ge=-10**15,le=10**15)]
Money=Annotated[StrictInt,Field(ge=0,le=10**15)]

class AdjustmentInput(UploadInput):
    adjustment_id:Annotated[StrictStr,Field(min_length=1,max_length=80)]
    label:Annotated[StrictStr,Field(min_length=1,max_length=160)]
    amount:SignedMoney
    evidence_ids:Annotated[list[StrictStr],Field(min_length=1,max_length=30)]
    authority_id:StrictStr

class CorporateWorkpaperInput(UploadInput):
    year_start:date
    year_end:date
    category:Literal['general_for_profit','nonprofit_taxable_business','special_small_corporation','cooperative_special_article72']
    book_profit:SignedMoney
    book_evidence_ids:Annotated[list[StrictStr],Field(min_length=1,max_length=30)]
    adjustments:Annotated[list[AdjustmentInput],Field(max_length=300)]
    approved_loss_use:Money
    loss_evidence_ids:Annotated[list[StrictStr],Field(max_length=30)]=[]
    idempotency_key:Annotated[StrictStr,Field(pattern=r'^[A-Za-z0-9_-]{8,120}$')]
    @model_validator(mode='after')
    def valid_adjustments(self):
        if len({a.adjustment_id for a in self.adjustments})!=len(self.adjustments):raise ValueError('조정 ID 중복')
        if self.approved_loss_use and not self.loss_evidence_ids:raise ValueError('결손금 사용에는 근거가 필요합니다.')
        return self

class CarryforwardInput(UploadInput):
    label:Annotated[StrictStr,Field(min_length=1,max_length=120)]
    opening:Money
    additions:Money
    used:Money
    expired:Money
    period_end:date
    evidence_ids:Annotated[list[StrictStr],Field(min_length=1,max_length=30)]
    idempotency_key:Annotated[StrictStr,Field(pattern=r'^[A-Za-z0-9_-]{8,120}$')]

class PayComponent(UploadInput):
    code:Annotated[StrictStr,Field(min_length=1,max_length=50)]
    amount:Money
    evidence_id:StrictStr

class PayrollReconciliationInput(UploadInput):
    worker_reference:Annotated[StrictStr,Field(min_length=1,max_length=80)]
    accrual_month:Annotated[StrictStr,Field(pattern=r'^20[0-9]{2}-(?:0[1-9]|1[0-2])$')]
    payment_date:date
    earnings:Annotated[list[PayComponent],Field(min_length=1,max_length=50)]
    deductions:Annotated[list[PayComponent],Field(max_length=50)]
    expected_net_payment:Money
    idempotency_key:Annotated[StrictStr,Field(pattern=r'^[A-Za-z0-9_-]{8,120}$')]
    @model_validator(mode='after')
    def unique_codes(self):
        for group in (self.earnings,self.deductions):
            if len({item.code for item in group})!=len(group):raise ValueError('급여 구성 항목이 중복되었습니다.')
        return self


class TaxWorkpapers:
    def __init__(self,service):self.service,self.db,self.auth=service,service.db,service.auth

    def _save(self,c,actor,company,kind,data,result):
        values=data.model_dump(mode='json');key=kind+'_'+digest({'company':company,'key':data.idempotency_key});h=digest(values)
        previous=c.execute('SELECT 1 FROM objects WHERE company_id=? AND kind=? AND id=?',(company,kind,key)).fetchone()
        if previous:
            old=self.db.get(c,company,kind,key)
            if old['input_hash']!=h:raise DomainError('WORKPAPER_KEY_CONFLICT','같은 계산 요청에 다른 입력이 있습니다.')
            return old
        row=self.db.create(c,company,kind,{'inputs':values,'result':result,'input_hash':h,'author':actor.id,'state':'REVIEW_WORKPAPER','statutory_return_complete':False,'submitted':False},ident=key)
        self.db.audit(c,company,actor.id,kind+'.created',key,{'input_hash':h})
        return row

    def corporate(self,actor,company,data:CorporateWorkpaperInput):
        data=CorporateWorkpaperInput.model_validate(data)
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('reviewer','preparer'))
            self.service._evidence_check(c,company,data.book_evidence_ids+data.loss_evidence_ids,required=True)
            authority_refs=[]
            for adjustment in data.adjustments:
                self.service._evidence_check(c,company,adjustment.evidence_ids,required=True)
                authority=self.db.get(c,company,'authority',adjustment.authority_id)
                if not authority.get('reviewed') or not authority['effective_from']<=data.year_end.isoformat()<=authority['effective_to']:
                    raise DomainError('ADJUSTMENT_AUTHORITY','세무조정 근거의 검토와 적용기간이 필요합니다.')
                self.auth.guard(c,Actor(authority['reviewed_by'],'rule-reviewer'),company,('owner','reviewer'))
                authority_refs.append({'id':authority['id'],'revision':authority['revision']})
            adjusted=data.book_profit+sum(item.amount for item in data.adjustments)
            if data.approved_loss_use>max(adjusted,0):raise DomainError('LOSS_USE_EXCEEDS_BASE','결손금 사용액이 양수 조정소득을 초과합니다.')
            base=max(0,adjusted-data.approved_loss_use)
            rate=progressive_component(base,data.category,data.year_start,data.year_end)
            return self._save(c,actor,company,'corporate_workpaper',data,{'book_profit':data.book_profit,'adjustment_total':sum(x.amount for x in data.adjustments),'adjusted_income':adjusted,'loss_used':data.approved_loss_use,'taxable_base_candidate':base,'rate_component':rate,'authority_refs':authority_refs,'tax_return_ready':False})

    def carryforward(self,actor,company,data:CarryforwardInput):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('reviewer','preparer'))
            self.service._evidence_check(c,company,data.evidence_ids,required=True)
            closing=data.opening+data.additions-data.used-data.expired
            if closing<0:raise DomainError('CARRYFORWARD_OVERUSE','기초와 증가액보다 사용·소멸액이 큽니다.')
            return self._save(c,actor,company,'carryforward_workpaper',data,{'closing':closing,'reconciliation':'opening+additions-used-expired','eligibility_or_expiry_rule_verified':False})

    def payroll(self,actor,company,data:PayrollReconciliationInput):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('reviewer',))
            self.service._evidence_check(c,company,[x.evidence_id for x in data.earnings+data.deductions],required=True)
            gross=sum(x.amount for x in data.earnings);deductions=sum(x.amount for x in data.deductions);net=gross-deductions
            if net<0 or net!=data.expected_net_payment:raise DomainError('PAYROLL_RECONCILIATION','급여·공제 합계와 실제 지급 대상액이 맞지 않습니다.')
            return self._save(c,actor,company,'payroll_workpaper',data,{'gross':gross,'deductions':deductions,'net_payment':net,'withholding_calculated':False,'insurance_rates_verified':False,'bank_payment_executed':False,'basis':'uploaded_reviewed_pay_components_not_tax_assumptions'})
