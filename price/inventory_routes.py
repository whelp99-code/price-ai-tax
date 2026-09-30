"""Company-scoped inventory review and ledger integration endpoints."""
from fastapi import Depends
from .auth import Actor
from .inventory_books import InventoryBooks,StockReceipt,StockIssue,StockReview,StockReturn


def register_inventory_routes(app,service,actor_dependency):
    stock=InventoryBooks(service);app.state.inventory=stock
    p='/api/companies/{company}'

    @app.get(p+'/owner/inventory')
    def balance(company:str,a:Actor=Depends(actor_dependency)):return stock.balance(a,company)

    @app.post(p+'/inventory/receipts',status_code=201)
    def receive(company:str,data:StockReceipt,a:Actor=Depends(actor_dependency)):return stock.receive(a,company,data)

    @app.post(p+'/inventory/receipts/{ident}/approve')
    def approve_receipt(company:str,ident:str,data:StockReview,a:Actor=Depends(actor_dependency)):return stock.approve_receipt(a,company,ident,data)

    @app.post(p+'/inventory/issues',status_code=201)
    def issue(company:str,data:StockIssue,a:Actor=Depends(actor_dependency)):return stock.issue(a,company,data)

    @app.post(p+'/inventory/issues/{ident}/approve')
    def approve_issue(company:str,ident:str,data:StockReview,a:Actor=Depends(actor_dependency)):return stock.approve_issue(a,company,ident,data)

    @app.post(p+'/inventory/issues/{ident}/post')
    def post_issue(company:str,ident:str,a:Actor=Depends(actor_dependency)):return stock.post_issue(a,company,ident)

    @app.post(p+'/inventory/returns',status_code=201)
    def propose_return(company:str,data:StockReturn,a:Actor=Depends(actor_dependency)):return stock.return_goods(a,company,data)

    @app.post(p+'/inventory/returns/{ident}/approve')
    def approve_return(company:str,ident:str,data:StockReview,a:Actor=Depends(actor_dependency)):return stock.approve_return(a,company,ident,data)

    @app.post(p+'/inventory/returns/{ident}/post')
    def post_return(company:str,ident:str,a:Actor=Depends(actor_dependency)):return stock.post_return(a,company,ident)

    @app.get(p+'/inventory/workspace')
    def inventory_workspace(company:str,a:Actor=Depends(actor_dependency)):
        with service.db.connect() as c:
            service.auth.guard(c,a,company,('owner','preparer','reviewer'))
            return {name:service.db.list(c,company,kind) for name,kind in [('receipts','stock_lot'),('issues','stock_issue'),('returns','stock_return')]}
