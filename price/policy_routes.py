"""Policy review workbench. HTTP policy posting remains unavailable until release signoff."""
from typing import Annotated
from fastapi import Depends
from pydantic import Field,StrictStr
from .auth import Actor
from .upload_models import UploadInput
from .autopolicies import PolicyEngine,PolicyInput,PolicyReview,PolicyAction

class EvaluationInput(UploadInput):
    event_id:Annotated[StrictStr,Field(min_length=1,max_length=160)]


def register_policy_routes(app,service,actor_dependency):
    engine=PolicyEngine(service,posting_enabled=False)
    app.state.policy_engine=engine
    prefix='/api/companies/{company}'

    @app.get(prefix+'/policies')
    def policies(company:str,a:Actor=Depends(actor_dependency)):
        with service.db.connect() as c:
            service.auth.guard(c,a,company)
            return {'policies':service.db.list(c,company,'auto_policy'),'execution_enabled':False,'mode':'review_and_shadow'}

    @app.post(prefix+'/policies',status_code=201)
    def create_policy(company:str,data:PolicyInput,a:Actor=Depends(actor_dependency)):
        return engine.create(a,company,data)

    @app.post(prefix+'/policies/{ident}/review')
    def review_policy(company:str,ident:str,data:PolicyReview,a:Actor=Depends(actor_dependency)):
        return engine.review(a,company,ident,data)

    @app.post(prefix+'/policies/{ident}/activate')
    def activate_policy(company:str,ident:str,data:PolicyAction,a:Actor=Depends(actor_dependency)):
        return engine.activate(a,company,ident,data)

    @app.post(prefix+'/policies/{ident}/evaluate')
    def evaluate_policy(company:str,ident:str,data:EvaluationInput,a:Actor=Depends(actor_dependency)):
        return engine.evaluate(a,company,ident,data.event_id)

    @app.post(prefix+'/policies/{ident}/revoke')
    def revoke_policy(company:str,ident:str,data:PolicyAction,a:Actor=Depends(actor_dependency)):
        return engine.revoke(a,company,ident,data)

    @app.post(prefix+'/posting-authorizations/{ident}/execute')
    def execute_policy(company:str,ident:str,a:Actor=Depends(actor_dependency)):
        with service.db.connect() as c:service.auth.guard(c,a,company,('owner','preparer'))
        return engine.execute(a,company,ident)
