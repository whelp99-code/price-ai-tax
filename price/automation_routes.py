"""Owner consent/visibility; the HTTP app never starts the posting executor."""
from fastapi import Depends
from .auth import Actor
from .automation import AutomaticBooks,AutomationConsent,AutomationAction


def register_automation_routes(app,service,actor_dependency):
    engine=AutomaticBooks(service,execution_enabled=False)
    app.state.automatic_books=engine
    p='/api/companies/{company}'

    @app.get(p+'/owner/automation')
    def status(company:str,a:Actor=Depends(actor_dependency)):
        return engine.status(a,company)

    @app.post(p+'/automation/consents',status_code=201)
    def consent(company:str,data:AutomationConsent,a:Actor=Depends(actor_dependency)):
        return engine.consent(a,company,data)

    @app.post(p+'/automation/consents/{ident}/stop')
    def stop(company:str,ident:str,data:AutomationAction,a:Actor=Depends(actor_dependency)):
        return engine.stop(a,company,ident,data)
