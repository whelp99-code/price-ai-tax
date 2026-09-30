"""Owner factual workflow endpoints; all company access is server checked."""
from fastapi import Depends
from .auth import Actor
from .casework import Casework,FactAnswer
from .event_settlements import EventSettlements,SettlementInput


def register_casework_routes(app,service,actor_dependency):
    work=Casework(service);settlements=EventSettlements(service)
    app.state.casework=work;app.state.event_settlements=settlements
    base='/api/companies/{company}'

    @app.get(base+'/owner/events')
    def owner_events(company:str,a:Actor=Depends(actor_dependency)):
        return {'events':work.list(a,company),'accounting_complete':False,'model_generated':False}

    @app.get(base+'/owner/questions')
    def owner_questions(company:str,a:Actor=Depends(actor_dependency)):
        events=work.list(a,company)
        questions=[{'event_id':e['id'],'expected_revision':e['revision'],'context_hash':e['context_hash'],
                    'date':e['values']['date'],'gross':e['values']['gross'],'currency':e['values']['currency'],
                    'counterparty':e['display_counterparty'],**q} for e in events for q in e['questions']]
        return {'questions':questions,'review_connection_needed':sum(e['state']=='REVIEW_CONNECTION_NEEDED' for e in events),'source_changed':sum(e['state']=='SOURCE_CHANGED' for e in events),'expert_assignee':None}

    @app.post(base+'/events/{ident}/facts')
    def answer_fact(company:str,ident:str,data:FactAnswer,a:Actor=Depends(actor_dependency)):
        return work.answer(a,company,ident,data)

    @app.get(base+'/events/{ident}/matches')
    def event_matches(company:str,ident:str,a:Actor=Depends(actor_dependency)):
        return work.candidates(a,company,ident)

    @app.post(base+'/events/refresh')
    def refresh_events(company:str,a:Actor=Depends(actor_dependency)):
        return work.refresh(a,company)

    @app.post(base+'/event-settlements')
    def allocate(company:str,data:SettlementInput,a:Actor=Depends(actor_dependency)):
        return settlements.allocate(a,company,data)

    @app.post(base+'/event-settlements/reconcile')
    def reconcile(company:str,a:Actor=Depends(actor_dependency)):
        return settlements.reconcile_strong_references(a,company)

    @app.get(base+'/event-settlements')
    def get_settlements(company:str,a:Actor=Depends(actor_dependency)):
        return settlements.report(a,company)
