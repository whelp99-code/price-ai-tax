"""Integrated accounting operations, review workpapers and release-readiness APIs.

Owner screens consume summaries; specialist inputs stay on the review API.
No API caller can install an adapter or enable live financial execution.
"""
from datetime import date
from fastapi import Depends
from .auth import Actor
from .finance_ops import FinanceOps,ScheduleInput
from .tax_workpapers import TaxWorkpapers,CorporateWorkpaperInput,CarryforwardInput,PayrollReconciliationInput
from .review_cases import ReviewCases,ReviewRequest,ReviewAssignment,ReviewFinding,ReviewRevision
from .submission_gateway import SubmissionGateway,PackageInput,PackageRevision
from .release_readiness import ReleaseReadiness
from .pipeline_health import PipelineHealth
from .discrepancies import Discrepancies


def register_operations_routes(app,service,actor_dependency):
    finance=FinanceOps(service);tax=TaxWorkpapers(service);reviews=ReviewCases(service)
    gateway=SubmissionGateway(service);release=ReleaseReadiness(service)
    pipeline=PipelineHealth(service)
    discrepancies=Discrepancies(service)
    app.state.finance_ops=finance;app.state.tax_workpapers=tax;app.state.review_cases=reviews
    app.state.submission_gateway=gateway;app.state.release_readiness=release
    p='/api/companies/{company}'

    @app.get(p+'/discrepancies')
    def list_discrepancies(company:str,a:Actor=Depends(actor_dependency)):
        return discrepancies.list(a,company)

    @app.post(p+'/discrepancies/{ident}/invalidate')
    def invalidate_discrepancy(company:str,ident:str,a:Actor=Depends(actor_dependency)):
        return discrepancies.invalidate_changed_source(a,company,ident)

    @app.get(p+'/owner/reports')
    def owner_report(company:str,start:date,end:date,a:Actor=Depends(actor_dependency)):
        return finance.report(a,company,start,end)

    @app.post(p+'/accounting-schedules',status_code=201)
    def schedule(company:str,data:ScheduleInput,a:Actor=Depends(actor_dependency)):
        return finance.propose_schedule(a,company,data)

    @app.post(p+'/workpapers/corporate',status_code=201)
    def corporate(company:str,data:CorporateWorkpaperInput,a:Actor=Depends(actor_dependency)):
        return tax.corporate(a,company,data)

    @app.post(p+'/workpapers/carryforward',status_code=201)
    def carryforward(company:str,data:CarryforwardInput,a:Actor=Depends(actor_dependency)):
        return tax.carryforward(a,company,data)

    @app.post(p+'/workpapers/payroll',status_code=201)
    def payroll(company:str,data:PayrollReconciliationInput,a:Actor=Depends(actor_dependency)):
        return tax.payroll(a,company,data)

    @app.get(p+'/expert-reviews')
    def list_reviews(company:str,a:Actor=Depends(actor_dependency)):
        return {'reviews':reviews.list(a,company),'professional_credentials_verified':False}

    @app.post(p+'/expert-reviews',status_code=201)
    def request_review(company:str,data:ReviewRequest,a:Actor=Depends(actor_dependency)):
        return reviews.open(a,company,data)

    @app.post(p+'/expert-reviews/{ident}/assign')
    def assign_review(company:str,ident:str,data:ReviewAssignment,a:Actor=Depends(actor_dependency)):
        return reviews.assign(a,company,ident,data)

    @app.post(p+'/expert-reviews/{ident}/accept')
    def accept_review(company:str,ident:str,data:ReviewRevision,a:Actor=Depends(actor_dependency)):
        return reviews.accept(a,company,ident,data)

    @app.post(p+'/expert-reviews/{ident}/finding')
    def finding(company:str,ident:str,data:ReviewFinding,a:Actor=Depends(actor_dependency)):
        return reviews.conclude(a,company,ident,data)

    @app.post(p+'/expert-reviews/{ident}/verify')
    def verify_finding(company:str,ident:str,data:ReviewRevision,a:Actor=Depends(actor_dependency)):
        return reviews.verify(a,company,ident,data)

    @app.get(p+'/submission-packages')
    def packages(company:str,a:Actor=Depends(actor_dependency)):
        return gateway.list(a,company)

    @app.post(p+'/submission-packages',status_code=201)
    def prepare_package(company:str,data:PackageInput,a:Actor=Depends(actor_dependency)):
        return gateway.prepare(a,company,data)

    @app.post(p+'/submission-packages/{ident}/review')
    def review_package(company:str,ident:str,data:PackageRevision,a:Actor=Depends(actor_dependency)):
        return gateway.review(a,company,ident,data)

    @app.post(p+'/submission-packages/{ident}/authorize')
    def authorize_package(company:str,ident:str,data:PackageRevision,a:Actor=Depends(actor_dependency)):
        return gateway.authorize(a,company,ident,data)

    @app.post(p+'/submission-packages/{ident}/submit')
    def submit_package(company:str,ident:str,a:Actor=Depends(actor_dependency)):
        return gateway.submit(a,company,ident)

    @app.post(p+'/submission-packages/{ident}/reconcile')
    def reconcile_package(company:str,ident:str,a:Actor=Depends(actor_dependency)):
        return gateway.reconcile(a,company,ident)

    @app.get(p+'/ops/release-readiness')
    def release_state(company:str,a:Actor=Depends(actor_dependency)):
        return release.report(a,company)

    @app.get(p+'/ops/pipeline')
    def pipeline_state(company:str,a:Actor=Depends(actor_dependency)):
        return pipeline.report(a,company)
