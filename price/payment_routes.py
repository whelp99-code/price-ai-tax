"""Payment accounting from bank evidence; never an instruction to a bank."""
from fastapi import Depends
from .auth import Actor
from .payment_books import PaymentBooks,PaymentRequest,PaymentReview


def register_payment_routes(app,service,actor_dependency):
    engine=PaymentBooks(service);app.state.payment_books=engine
    p='/api/companies/{company}/payment-books'
    @app.get(p)
    def listing(company:str,a:Actor=Depends(actor_dependency)):return engine.list(a,company)
    @app.post(p,status_code=201)
    def propose(company:str,data:PaymentRequest,a:Actor=Depends(actor_dependency)):return engine.propose(a,company,data)
    @app.post(p+'/{ident}/approve')
    def approve(company:str,ident:str,data:PaymentReview,a:Actor=Depends(actor_dependency)):return engine.approve(a,company,ident,data)
    @app.post(p+'/{ident}/post')
    def post(company:str,ident:str,a:Actor=Depends(actor_dependency)):return engine.post(a,company,ident)
