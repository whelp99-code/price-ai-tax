"""Reviewed compensation ledger and resident withholding calculation routes."""
from fastapi import Depends,Query
from .auth import Actor
from .withholding_rules import WithholdingPayment,calculate_withholding
from .compensation_books import CompensationBooks,CompensationInput,CompensationReview


def register_compensation_routes(app,service,actor_dependency):
    engine=CompensationBooks(service);app.state.compensation_books=engine
    p='/api/companies/{company}'
    @app.post(p+'/compensation/calculate')
    def calculation(company:str,data:WithholdingPayment,a:Actor=Depends(actor_dependency)):
        with service.db.connect() as c:service.auth.guard(c,a,company,('reviewer',))
        return calculate_withholding(data)
    @app.post(p+'/compensation',status_code=201)
    def propose(company:str,data:CompensationInput,a:Actor=Depends(actor_dependency)):
        return engine.propose(a,company,data)
    @app.get(p+'/compensation')
    def records(company:str,a:Actor=Depends(actor_dependency)):
        return engine.list(a,company)
    @app.post(p+'/compensation/{ident}/review')
    def review(company:str,ident:str,data:CompensationReview,a:Actor=Depends(actor_dependency)):
        return engine.review(a,company,ident,data)
    @app.post(p+'/compensation/{ident}/post')
    def post(company:str,ident:str,a:Actor=Depends(actor_dependency)):
        return engine.post(a,company,ident)
    @app.get(p+'/compensation/statement/monthly')
    def statement(company:str,year:int=Query(...,ge=2000,le=2100),month:int=Query(...,ge=1,le=12),a:Actor=Depends(actor_dependency)):
        return engine.monthly_statement(a,company,year,month)
