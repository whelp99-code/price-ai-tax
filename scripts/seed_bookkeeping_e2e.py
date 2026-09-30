"""Create an isolated synthetic bookkeeping scenario; never touches an existing DB."""
from pathlib import Path
from datetime import date
import argparse,base64,json,sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from price.db import Database
from price.service import Service
from price.models import CompanyInput,UserInput,TransactionInput,PeriodInput
from price.auth import Actor
from price.book_schedules import BookSchedules,BookScheduleInput
from price.inventory_books import InventoryBooks,StockReceipt,StockIssue,StockReview


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--folder',required=True);args=parser.parse_args()
    folder=Path(args.folder).resolve()
    if not folder.is_relative_to(ROOT/'.data') or not folder.is_dir():raise RuntimeError('Use a new test directory in project .data')
    dbpath=folder/'books.sqlite3'
    if dbpath.exists():raise RuntimeError('Existing database not overwritten')
    svc=Service(Database(dbpath))
    password='Synthetic-Books-Only-2026!'
    company,owner=svc.auth.bootstrap(CompanyInput(name='검증 전용 법인',industry='SYNTHETIC ONLY'),'books-owner',password,demo=True)
    rr=svc.add_user(owner,company,UserInput(username='books-reviewer',password=password,role='reviewer'));reviewer=Actor(rr['id'],'books-reviewer')
    evidence=svc.upload_evidence(owner,company,'시험전용자료.txt',base64.b64encode('SYNTHETIC ONLY. 실제 은행·세금계산서가 아닙니다.'.encode()).decode())
    def post(ref,net,tax,direction='purchase',when='2026-07-01',use_type='expense'):
        tx=svc.create_transaction(owner,company,TransactionInput(external_id=ref,event_date=when,direction=direction,counterparty='가상 거래처',net=net,tax=tax,gross=net+tax,tax_category='standard' if tax else 'exempt',deductible=bool(tax),use_type=use_type,business_purpose='정해진 합성 시험',evidence_ids=[evidence['id']]))
        j=svc.analyze(owner,company,tx['id'])['journal'];j=svc.transition_journal(reviewer,company,j['id'],'approve',j['revision'],'격리 시험의 기대 전표 비교');return svc.transition_journal(owner,company,j['id'],'post',j['revision'])
    subscription=post('BOOKS-PREPAID',1001,100)
    asset=post('BOOKS-ASSET',3000,300)
    goods=post('BOOKS-INVENTORY',1001,0,use_type='resale')
    sale=post('BOOKS-SALE',1000,100,'sale','2026-08-10')
    schedules=BookSchedules(svc)
    prepaid=schedules.propose(owner,company,BookScheduleInput(kind='prepaid',source_journal_id=subscription['id'],label='합성 분기 구독',classification_date='2026-07-01',start='2026-07-01',end='2026-09-30',evidence_ids=[evidence['id']],idempotency_key='fixture-prepaid-plan'))
    equipment=schedules.propose(owner,company,BookScheduleInput(kind='asset',source_journal_id=asset['id'],label='합성 회사 장비',classification_date='2026-07-01',start='2026-07-01',months=3,salvage=0,evidence_ids=[evidence['id']],idempotency_key='fixture-asset-plan'))
    inventory=InventoryBooks(svc)
    lot=inventory.receive(owner,company,StockReceipt(source_journal_id=goods['id'],sku='DEMO-SERVER',quantity=3,received_on='2026-07-01',evidence_ids=[evidence['id']],reference='BOOKS-GOODS'))
    inventory.approve_receipt(reviewer,company,lot['id'],StockReview(expected_revision=lot['revision'],reason='시험 수량 3, 원가1001 확인'))
    issue=inventory.issue(owner,company,StockIssue(sale_journal_id=sale['id'],sku='DEMO-SERVER',quantity=1,shipped_on='2026-08-10',evidence_ids=[evidence['id']],reference='BOOKS-SHIPMENT'))
    issue=inventory.approve_issue(reviewer,company,issue['id'],StockReview(expected_revision=issue['revision'],reason='판매·출고량 대조'));inventory.post_issue(owner,company,issue['id'])
    period=svc.create_period(owner,company,PeriodInput(label='합성 7~8월',start='2026-07-01',end='2026-08-31'))
    csv=('합성 전기말 잔액\n기준일: 2026-07-01\n통화: KRW\n계정과목,차변잔액,대변잔액\n보통예금,1000000,0\n자본금,0,1000000\n합계,1000000,1000000\n')
    (folder/'기초잔액_합성.csv').write_text(csv,encoding='utf-8')
    result={'db':str(dbpath),'company':company,'prepaid_id':prepaid['id'],'asset_id':equipment['id'],'period_id':period['id'],'opening_file':str(folder/'기초잔액_합성.csv'),'expected_august':{'income':1000,'expenses':1670,'net_profit':-670,'cash':1000000,'stock_value':668,'prepaid_remaining':327,'asset_remaining':1000},'synthetic_only':True}
    (folder/'fixture.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
