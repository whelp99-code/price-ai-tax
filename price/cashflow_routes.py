"""Company-scoped card/own-account reconciliation and expert review routes."""
from fastapi import Depends
from .auth import Actor
from .card_books import CardBooks,CardBookInput,CardBookReview
from .internal_transfers import InternalTransfers,TransferReview
from .source_availability import SourceAvailability
from datetime import date


def register_cashflow_routes(app,service,actor_dependency):
    cards,transfers=CardBooks(service),InternalTransfers(service)
    app.state.card_books=cards;app.state.internal_transfers=transfers
    p='/api/companies/{company}'

    @app.get(p+'/owner/source-availability')
    def availability(company:str,start:date,end:date,a:Actor=Depends(actor_dependency)):
        return SourceAvailability(service).report(a,company,start,end)

    @app.get(p+'/owner/card-books')
    def card_summary(company:str,a:Actor=Depends(actor_dependency)):return cards.list(a,company)

    @app.post(p+'/card-books',status_code=201)
    def card_propose(company:str,data:CardBookInput,a:Actor=Depends(actor_dependency)):return cards.propose(a,company,data)

    @app.get(p+'/card-books/{ident}')
    def card_detail(company:str,ident:str,a:Actor=Depends(actor_dependency)):
        with service.db.connect() as c:
            service.auth.guard(c,a,company)
            return service.db.get(c,company,'card_book',ident)

    @app.post(p+'/card-books/{ident}/approve')
    def card_review(company:str,ident:str,data:CardBookReview,a:Actor=Depends(actor_dependency)):return cards.approve(a,company,ident,data)

    @app.post(p+'/card-books/{ident}/post')
    def card_post(company:str,ident:str,a:Actor=Depends(actor_dependency)):return cards.post(a,company,ident)

    @app.get(p+'/owner/internal-transfers')
    def transfer_summary(company:str,a:Actor=Depends(actor_dependency)):return transfers.list(a,company)

    @app.post(p+'/internal-transfers/refresh')
    def transfer_refresh(company:str,a:Actor=Depends(actor_dependency)):return transfers.refresh(a,company)

    @app.get(p+'/internal-transfers/{ident}')
    def transfer_detail(company:str,ident:str,a:Actor=Depends(actor_dependency)):
        with service.db.connect() as c:
            service.auth.guard(c,a,company)
            return service.db.get(c,company,'internal_transfer',ident)

    @app.post(p+'/internal-transfers/{ident}/approve')
    def transfer_review(company:str,ident:str,data:TransferReview,a:Actor=Depends(actor_dependency)):return transfers.approve(a,company,ident,data)

    @app.post(p+'/internal-transfers/{ident}/post')
    def transfer_post(company:str,ident:str,a:Actor=Depends(actor_dependency)):return transfers.post(a,company,ident)
