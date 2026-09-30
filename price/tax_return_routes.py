"""Tenant-scoped structured VAT workpapers; no live filing side effects."""
from fastapi import Depends
from .auth import Actor
from .vat_returns import VatReturns,VatBuild,VatAdjustmentInput,VatReview
from .vat_rules_kr import InputTaxFacts,CommonInput,assess_input_vat,apportion_common_input


def register_tax_return_routes(app,service,actor_dependency):
    engine=VatReturns(service);app.state.vat_returns=engine
    p='/api/companies/{company}'

    @app.post(p+'/vat/returns',status_code=201)
    def build(company:str,data:VatBuild,a:Actor=Depends(actor_dependency)):return engine.build(a,company,data)

    @app.get(p+'/vat/returns')
    def returns(company:str,a:Actor=Depends(actor_dependency)):return engine.list(a,company)

    @app.post(p+'/vat/returns/{ident}/review')
    def review(company:str,ident:str,data:VatReview,a:Actor=Depends(actor_dependency)):return engine.review(a,company,ident,data)

    @app.post(p+'/vat/adjustments',status_code=201)
    def adjustment(company:str,data:VatAdjustmentInput,a:Actor=Depends(actor_dependency)):return engine.add_adjustment(a,company,data)

    @app.post(p+'/vat/adjustments/{ident}/review')
    def adjustment_review(company:str,ident:str,data:VatReview,a:Actor=Depends(actor_dependency)):return engine.review_adjustment(a,company,ident,data)

    @app.post(p+'/vat/input-assessment')
    def input_assessment(company:str,data:InputTaxFacts,a:Actor=Depends(actor_dependency)):
        with service.db.connect() as c:service.auth.guard(c,a,company,('reviewer',))
        return assess_input_vat(data)

    @app.post(p+'/vat/common-input')
    def common(company:str,data:CommonInput,a:Actor=Depends(actor_dependency)):
        with service.db.connect() as c:service.auth.guard(c,a,company,('reviewer',))
        return apportion_common_input(data)

    @app.get(p+'/owner/tax-summary')
    def owner_tax(company:str,a:Actor=Depends(actor_dependency)):
        rows=engine.list(a,company)
        return {'items':[{'id':r['id'],'start':r['start'],'end':r['end'],'state':r['state'],
                         'output_vat':r['totals']['output_vat'],'input_vat':r['totals']['deductible_input_vat'],
                         'estimated_tax_due_or_refund':r['totals']['tax_due_or_refund_before_payment_rounding'],
                         'unresolved_count':len(r['blockers']),'official_return_ready':False,
                         'notice':'장부와 검토된 조정으로 만든 예상 검토 금액입니다. 신고 접수·납부 결과가 아닙니다.'} for r in rows],
                'live_filing':False}
