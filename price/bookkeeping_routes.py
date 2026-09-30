"""Authenticated book-schedule service. Approval is separate from factual input."""
from datetime import date
from fastapi import Depends
from pydantic import StrictStr
from .auth import Actor
from .book_schedules import BookSchedules,BookScheduleInput,ScheduleReview
from .upload_models import UploadInput
from .opening_books import OpeningBooks,OpeningRequest,OpeningReview
from .financial_statements import FinancialStatements


class PostThrough(UploadInput):
    through:date


def register_bookkeeping_routes(app,service,actor_dependency):
    engine=BookSchedules(service)
    openings=OpeningBooks(service)
    app.state.book_schedules=engine
    root='/api/companies/{company}'

    @app.get(root+'/owner/financial-statements')
    def statements(company:str,start:date,end:date,a:Actor=Depends(actor_dependency)):
        return FinancialStatements(service).build(a,company,start,end)

    @app.post(root+'/opening-balances',status_code=201)
    def propose_opening(company:str,data:OpeningRequest,a:Actor=Depends(actor_dependency)):
        return openings.propose(a,company,data)

    @app.get(root+'/opening-balances')
    def list_openings(company:str,a:Actor=Depends(actor_dependency)):
        return openings.list(a,company)

    @app.post(root+'/opening-balances/{ident}/approve')
    def approve_opening(company:str,ident:str,data:OpeningReview,a:Actor=Depends(actor_dependency)):
        return openings.approve(a,company,ident,data)

    @app.post(root+'/opening-balances/{ident}/post')
    def post_opening(company:str,ident:str,a:Actor=Depends(actor_dependency)):
        return openings.post(a,company,ident)

    @app.post(root+'/book-schedules',status_code=201)
    def propose(company:str,data:BookScheduleInput,a:Actor=Depends(actor_dependency)):
        return engine.propose(a,company,data)

    @app.get(root+'/book-schedules')
    def list_plans(company:str,a:Actor=Depends(actor_dependency)):
        return engine.list(a,company)

    @app.post(root+'/book-schedules/{ident}/approve')
    def approve(company:str,ident:str,data:ScheduleReview,a:Actor=Depends(actor_dependency)):
        return engine.approve(a,company,ident,data)

    @app.post(root+'/book-schedules/{ident}/post-due')
    def post_due(company:str,ident:str,data:PostThrough,a:Actor=Depends(actor_dependency)):
        return engine.post_due(a,company,ident,data.through)

    @app.post(root+'/book-schedules/{ident}/cancel')
    def cancel(company:str,ident:str,data:ScheduleReview,a:Actor=Depends(actor_dependency)):
        return engine.cancel(a,company,ident,data)

    @app.get(root+'/owner/book-schedules')
    def owner_summary(company:str,a:Actor=Depends(actor_dependency)):
        plans=engine.list(a,company)
        items=[{'id':p['id'],'label':p['input']['label'],'kind':p['input']['kind'],'state':p['state'],
                'original_cost':p['cost'],'recognized_expense':p['recognized_expense'],'remaining_value':p['book_value'],
                'remaining_period_entries':p['remaining_entries'],'posted_count':len(p['posted_journal_ids']),
                'correction_needed':bool(p['reversed_journal_ids']),'tax_depreciation_verified':False} for p in plans]
        return {'items':items,'notice':'검토된 계약기간·자산 비용의 장부 반영 결과입니다. 세무상 공제 금액과는 별도로 관리합니다.'}
