from datetime import date
import subprocess,sys,json
import pytest
from test_owner_upload_api import web
from test_book_schedules import approved,make_plan
from test_vat_returns import ready_period
from test_compensation_books import fixture,review
from price.book_schedules import ScheduleReview
from price.db import Database
from price.service import Service


def test_owner_views_and_expert_source_forms_are_separate(web):
    app,client,p=web
    root=client.get('/').text
    assert 'book_results.js' in root and 'casework.js' in root
    assert 'bookkeeping.js' in client.get('/bookkeeping').text
    assert client.get('/static/book_results.css').status_code==200
    assert client.get(p+'/owner/inventory').status_code==200
    assert client.get(p+'/owner/book-schedules').status_code==200
    assert client.get(p+'/owner/tax-summary').status_code==200
    assert client.get(p+'/owner/financial-statements?start=2026-09-01&end=2026-09-30').status_code==200
    assert client.get(p+'/compensation/statement/monthly?year=2026&month=9').status_code==200


def test_schedule_api_json_body_and_server_fields_checked(web,w):
    app,client,p=web;engine,plan,_=make_plan(w)
    response=client.post(p+'/book-schedules',json=plan['input'])
    assert response.status_code==201 and response.json()['id']==plan['id']
    assert client.post(p+'/book-schedules',json={**plan['input'],'state':'APPROVED','reviewed_by':w.owner.id}).status_code==422
    assert client.post(p+'/book-schedules/'+plan['id']+'/approve',json={'expected_revision':plan['revision'],'reason':'self'}).status_code==403


def test_vat_api_generates_structured_data_not_statutory_file(web,w):
    app,client,p=web;period=ready_period(w)
    response=client.post(p+'/vat/returns',json={'period_id':period['id']})
    assert response.status_code==201,response.text
    body=response.json();assert body['totals']['output_vat']==1000 and not body['official_form']
    assert client.get(p+'/owner/tax-summary').json()['items'][0]['estimated_tax_due_or_refund']==600
    assert client.post(p+'/vat/returns/'+body['id']+'/review',json={'expected_revision':body['revision'],'reason':'self'}).status_code==403


def test_owner_cannot_supply_tax_facts_through_expert_calculation_route(web):
    app,client,p=web
    facts={'category':'resident_personal_service_business','paid_on':'2026-09-20','gross':100000,'resident':True,'income_classification_confirmed':True}
    assert client.post(p+'/compensation/calculate',json=facts).status_code==403


def test_compensation_http_body_is_validated_and_writes_are_csrf_guarded(web,w):
    app,client,p=web;engine,data,a=fixture(w)
    response=client.post(p+'/compensation',json=data.model_dump(mode='json'))
    assert response.status_code==201,response.text
    assert response.json()['state']=='DRAFT'
    assert client.post(p+'/compensation',json=data.model_dump(mode='json'),headers={'x-csrf-token':'wrong'}).status_code==403
    assert client.post(p+'/compensation',json={**data.model_dump(mode='json'),'approved':True}).status_code==422


def test_actual_separate_schedule_worker_posts_approved_due_entries_only(w):
    # A real child process, using this test's isolated DB. Its source/model worker is unaffected.
    engine,plan,original=make_plan(w,source_date=date(2026,7,1),start=date(2026,7,1),end=date(2026,7,31),classification_date=date(2026,7,1))
    command=[sys.executable,'-m','price.book_schedule_worker','--db',str(w.db.path),'--once']
    first=subprocess.run(command,capture_output=True,text=True,timeout=15)
    assert first.returncode==0,first.stderr
    with w.db.connect() as c:assert len(w.db.list(c,w.company,'journal'))==1
    engine.approve(w.reviewer,w.company,plan['id'],ScheduleReview(expected_revision=plan['revision'],reason='실제 원거래와 7월 일정 독립 검증'))
    second=subprocess.run(command,capture_output=True,text=True,timeout=15)
    assert second.returncode==0,second.stderr
    assert json.loads(second.stdout.strip().splitlines()[-1])['journal_count']==2
    repeat=subprocess.run(command,capture_output=True,text=True,timeout=15)
    assert repeat.returncode==0,repeat.stderr
    with w.db.connect() as c:
        assert len(w.db.list(c,w.company,'journal'))==3
        assert w.db.get(c,w.company,'journal',original['id'])==original
        assert w.db.verify_audit(c,w.company)['valid']
    assert not json.loads(repeat.stdout.strip().splitlines()[-1])['processed']
