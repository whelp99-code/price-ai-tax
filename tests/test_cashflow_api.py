from datetime import date
import subprocess,sys,json
from fastapi.testclient import TestClient
import pytest
from price.api import create_app
from price.card_books import CardBooks
from price.db import DomainError
from test_owner_upload_api import web
from test_card_books import setup as card_setup,read,BANK_HEADER,BANK,CARD
from test_internal_transfers import setup as transfer_setup
from conftest import PASSWORD


def reviewer_client(w):
    client=TestClient(create_app(w.db.path))
    session=client.post('/api/login',json={'username':w.reviewer.username,'password':PASSWORD})
    assert session.status_code==200
    client.headers['x-csrf-token']=session.json()['csrf']
    return client


def test_cash_workbench_has_no_bank_execution_routes(web):
    app,client,p=web
    assert 'cashflow.js' in client.get('/cashflow').text
    assert client.get('/static/cashflow.js').status_code==200
    assert client.get(p+'/owner/card-books').json()['balances']=={}
    assert client.get(p+'/owner/internal-transfers').json()['items']==[]
    assert '/api/companies/{company}/bank-transfer' not in app.openapi()['paths']
    assert client.post(p+'/bank-transfer',json={}).status_code in (404,405)


def test_internal_transfer_full_http_pipeline_uses_independent_reviewer(web,w):
    app,client,p=web;e,_=transfer_setup(w)
    plan=client.get(p+'/owner/internal-transfers').json()['items'][0]
    assert client.post(p+'/internal-transfers/'+plan['id']+'/approve',json={'expected_revision':plan['revision'],'reason':'self'}).status_code==403
    with reviewer_client(w) as reviewer:
        approval=reviewer.post(p+'/internal-transfers/'+plan['id']+'/approve',json={'expected_revision':plan['revision'],'reason':'두 계좌·원본 대조'})
        assert approval.status_code==200,approval.text
    result=client.post(p+'/internal-transfers/'+plan['id']+'/post',json={})
    assert result.status_code==200 and len(result.json()['journal_ids'])==2
    report=client.get(p+'/owner/financial-statements?start=2026-09-01&end=2026-09-30').json()
    assert report['cash']['received']==0 and report['cash']['paid']==0
    assert report['cash']['internal_transfers']['received']==1000


def test_card_worker_proposes_charge_and_bill_without_posting(web,w):
    app,client,p=web;e,invoice,card,_=card_setup(w)
    result=e.run_once(w.company)
    assert result['state']=='PROPOSED' and result['operation']=='invoice_charge'
    charge=client.get(p+'/owner/card-books').json()['items'][0]
    assert charge['state']=='DRAFT'
    with reviewer_client(w) as reviewer:
        result=reviewer.post(p+'/card-books/'+charge['id']+'/approve',json={'expected_revision':charge['revision'],'reason':'원매입·카드 참조 대조'})
        assert result.status_code==200,result.text
    assert client.post(p+'/card-books/'+charge['id']+'/post',json={}).status_code==200
    raw=(BANK_HEADER+f'2026-09-03,카드대금,0,1100,{BANK},{CARD},KRW,CARD-BILL-HTTP\n').encode()
    work=read(w,raw,'card-http-bank')
    assert e.run_once(w.company)['operation']=='billing'
    bill=next(x for x in client.get(p+'/owner/card-books').json()['items'] if x['operation']=='billing')
    bank_event=next(x for x in work.list(w.owner,w.company) if x['kind']=='bank')
    assert bank_event['facts']['cash_reason']=='card_payment' and not bank_event['questions']
    assert client.post(p+'/card-books/'+bill['id']+'/approve',json={'expected_revision':bill['revision'],'reason':'self'}).status_code==403
    assert client.post(p+'/card-books/'+bill['id']+'/post',json={}).status_code==409
    with reviewer_client(w) as reviewer:
        result=reviewer.post(p+'/card-books/'+bill['id']+'/approve',json={'expected_revision':bill['revision'],'reason':'실제 은행 출금과 사용명세 대조'})
        assert result.status_code==200,result.text
    assert client.post(p+'/card-books/'+bill['id']+'/post',json={}).status_code==200
    assert client.get(p+'/owner/card-books').json()['balances'][CARD]==0


def test_cashflow_changes_keep_csrf_and_extra_field_rejection(web,w):
    app,client,p=web;e,_=transfer_setup(w);plan=e.list(w.owner,w.company)['items'][0]
    assert client.post(p+'/internal-transfers/'+plan['id']+'/post',json={},headers={'x-csrf-token':'invalid'}).status_code==403
    assert client.post(p+'/card-books',json={'operation':'billing','source_event_id':'x','statement_plan_ids':['a'],'reviewed_by':w.owner.id}).status_code==422
    with TestClient(create_app(w.db.path)) as anonymous:assert anonymous.get(p+'/owner/card-books').status_code==401


def test_real_source_worker_discovers_transfer_but_never_posts(w):
    from price.owner import OwnerView,CompanyFactsInput
    from price.ingestion import Ingestion
    from price.casework import Casework
    from test_internal_transfers import data,A,B
    from test_source_ingestion import upload
    reader=Ingestion(w.svc)
    OwnerView(w.svc,reader.uploads).confirm(w.owner,w.company,CompanyFactsInput(expected_revision=0,name='합성회사',mode='company',confirmed=True,instruments=[{'kind':'bank','identifier':A},{'kind':'bank','identifier':B}]))
    upload(w,data())
    command=[sys.executable,'-m','price.worker','--db',str(w.db.path),'--once']
    for _ in range(4):
        run=subprocess.run(command,capture_output=True,text=True,timeout=10)
        assert run.returncode==0,run.stderr
    with w.db.connect() as c:
        plans=w.db.list(c,w.company,'internal_transfer')
        assert len(plans)==1 and plans[0]['state']=='DRAFT'
        assert not w.db.list(c,w.company,'journal')
