from datetime import date
from concurrent.futures import ThreadPoolExecutor
import pytest
from price.inventory_books import InventoryBooks,StockReceipt,StockIssue,StockReview,StockReturn
from price.db import DomainError


def receipt(w,cost=1001,quantity=3,key='stock-receipt-0001',sku='SERVER-A'):
    source=w.post(w.tx(direction='purchase',net=cost,tax=0,gross=cost,tax_category='exempt',deductible=False,use_type='resale',event_date=date(2026,9,1),external_id=key))
    stock=InventoryBooks(w.svc,today=lambda:date(2026,12,31))
    draft=stock.receive(w.owner,w.company,StockReceipt(source_journal_id=source['id'],sku=sku,quantity=quantity,received_on=date(2026,9,1),evidence_ids=[w.evidence],reference=key))
    lot=stock.approve_receipt(w.reviewer,w.company,draft['id'],StockReview(expected_revision=draft['revision'],reason='수량·입고·원가 확인'))
    return stock,lot,source


def issue(w,stock,quantity=1,reference='delivery-0001',sku='SERVER-A',when=date(2026,9,10)):
    source=w.post(w.tx(net=1000,tax=100,gross=1100,event_date=when,external_id=reference))
    value=stock.issue(w.owner,w.company,StockIssue(sale_journal_id=source['id'],sku=sku,quantity=quantity,shipped_on=when,evidence_ids=[w.evidence],reference=reference))
    approved=stock.approve_issue(w.reviewer,w.company,value['id'],StockReview(expected_revision=value['revision'],reason='판매·출고·원가 검증'))
    return approved,source


def test_fifo_posts_cogs_and_conserves_every_won(w):
    stock,lot,original=receipt(w)
    costs=[]
    for n in range(3):
        order,_=issue(w,stock,reference='delivery-000'+str(n))
        done=stock.post_issue(w.owner,w.company,order['id'])
        costs.append(done['cost'])
    assert costs==[333,334,334] and sum(costs)==1001
    view=stock.balance(w.owner,w.company)
    assert view['items'][0]['quantity']==0 and view['items'][0]['value']==0
    with w.db.connect() as c:
        rows=w.db.list(c,w.company,'journal');assert w.db.get(c,w.company,'journal',original['id'])==original
        assert sum(l['amount'] for j in rows for l in j['lines'] if l['account']=='cost_of_sales' and l['side']=='debit')==1001
        assert w.db.verify_audit(c,w.company)['valid']


def test_receipt_only_registers_existing_inventory_not_duplicate_asset(w):
    stock,lot,original=receipt(w)
    with w.db.connect() as c:assert len(w.db.list(c,w.company,'journal'))==1
    assert stock.balance(w.owner,w.company)['items'][0]['value']==1001


def test_duplicate_receipt_and_delivery_are_idempotent(w):
    stock,lot,source=receipt(w)
    duplicate=stock.receive(w.owner,w.company,StockReceipt(**lot['input']))
    assert duplicate['id']==lot['id']
    with pytest.raises(DomainError):stock.receive(w.owner,w.company,StockReceipt(**{**lot['input'],'reference':'duplicate-source'}))
    order,sale=issue(w,stock)
    a=stock.post_issue(w.owner,w.company,order['id']);b=stock.post_issue(w.owner,w.company,order['id'])
    assert a['journal_id']==b['journal_id']
    assert stock.issue(w.owner,w.company,StockIssue(**order['input']))['id']==order['id']


def test_no_negative_stock_or_self_review(w):
    stock,lot,_=receipt(w)
    sale=w.post(w.tx(external_id='over-sale'))
    with pytest.raises(DomainError) as e:stock.issue(w.owner,w.company,StockIssue(sale_journal_id=sale['id'],sku='SERVER-A',quantity=4,shipped_on=date(2026,9,15),evidence_ids=[w.evidence],reference='over-delivery'))
    assert e.value.code=='INSUFFICIENT_STOCK'
    draft=stock.issue(w.owner,w.company,StockIssue(sale_journal_id=sale['id'],sku='SERVER-A',quantity=1,shipped_on=date(2026,9,15),evidence_ids=[w.evidence],reference='one-delivery'))
    with pytest.raises(DomainError) as e:stock.approve_issue(w.owner,w.company,draft['id'],StockReview(expected_revision=draft['revision'],reason='자기 확인'))
    assert e.value.code=='SELF_APPROVAL'


def test_two_approved_orders_cannot_overconsume_same_stock(w):
    stock,lot,_=receipt(w,quantity=2)
    a,_=issue(w,stock,quantity=2,reference='order-a')
    b,_=issue(w,stock,quantity=2,reference='order-b')
    stock.post_issue(w.owner,w.company,a['id'])
    with pytest.raises(DomainError):stock.post_issue(w.owner,w.company,b['id'])
    assert stock.balance(w.owner,w.company)['items'][0]['quantity']==0


def test_concurrent_duplicate_post_has_one_cost_journal(w):
    stock,lot,_=receipt(w);order,_=issue(w,stock)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:stock.post_issue(w.owner,w.company,order['id']),range(2)))
    assert results[0]['journal_id']==results[1]['journal_id']
    assert stock.balance(w.owner,w.company)['items'][0]['quantity']==2


def test_return_restores_original_cost_and_preserves_issue_history(w):
    stock,lot,_=receipt(w);order,_=issue(w,stock,quantity=2)
    order=stock.post_issue(w.owner,w.company,order['id'])
    ret=stock.return_goods(w.owner,w.company,StockReturn(issue_id=order['id'],returned_on=date(2026,9,20),evidence_ids=[w.evidence],reference='RMA-0001'))
    ret=stock.approve_return(w.reviewer,w.company,ret['id'],StockReview(expected_revision=ret['revision'],reason='반품 입고와 원가 확인'))
    posted=stock.post_return(w.owner,w.company,ret['id'])
    assert posted['cost']==667
    view=stock.balance(w.owner,w.company)['items'][0]
    assert view['quantity']==3 and view['value']==1001
    assert stock.post_return(w.owner,w.company,ret['id'])['id']==posted['id']
    again,_=issue(w,stock,quantity=3,reference='next-shipment',when=date(2026,9,21))
    assert stock.post_issue(w.owner,w.company,again['id'])['cost']==1001


def test_future_receipt_and_backdated_fifo_consumption_rejected(w):
    stock,lot,_=receipt(w)
    sale=w.post(w.tx(external_id='early-sale',event_date=date(2026,8,31)))
    with pytest.raises(DomainError):stock.issue(w.owner,w.company,StockIssue(sale_journal_id=sale['id'],sku='SERVER-A',quantity=1,shipped_on=date(2026,8,31),evidence_ids=[w.evidence],reference='early-shipment'))


def test_revoked_reviewer_cannot_authorize_execution(w):
    stock,lot,_=receipt(w);order,_=issue(w,stock)
    with w.db.transaction() as c:c.execute('DELETE FROM memberships WHERE company_id=? AND user_id=?',(w.company,w.reviewer.id))
    with pytest.raises(DomainError):stock.post_issue(w.owner,w.company,order['id'])
    assert stock.balance(w.owner,w.company)['items'][0]['quantity']==3


def test_unposted_cogs_blocks_month_close(w):
    stock,lot,_=receipt(w);order,_=issue(w,stock)
    p=w.period()
    assert any(b['code']=='STOCK_ISSUE_NOT_POSTED' for b in w.svc.readiness(w.owner,w.company,p['id'])['blockers'])


def test_other_company_stock_hidden(w):
    stock,_,_=receipt(w)
    with pytest.raises(DomainError):stock.balance(w.other_owner,w.company)
