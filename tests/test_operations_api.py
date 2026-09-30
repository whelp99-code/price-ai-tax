from datetime import date
import pytest
from fastapi.testclient import TestClient
from price.api import create_app
from conftest import PASSWORD
from test_casework import setup_sources,invoice
from test_source_ingestion import upload,BANK
from price.models import SubmitInput
from price.db import DomainError


@pytest.fixture
def api(w):
    app=create_app(w.db.path)
    with TestClient(app) as client:
        r=client.post('/api/login',json={'username':w.owner.username,'password':PASSWORD});assert r.status_code==200
        client.headers['x-csrf-token']=r.json()['csrf']
        yield app,client,'/api/companies/'+w.company


def test_owner_factual_question_http_flow_without_accounting_fields(w,api):
    app,client,p=api;work=setup_sources(w);event=invoice(work,w)
    questions=client.get(p+'/owner/questions').json()['questions']
    q=next(x for x in questions if x['event_id']==event['id'])
    assert q['key']=='business_use' and '계정과목' not in q['text']
    r=client.post(p+'/events/'+event['id']+'/facts',json={'expected_revision':q['expected_revision'],'context_hash':q['context_hash'],'question':q['key'],'answer':'business','idempotency_key':'owner-http-fact-01'})
    assert r.status_code==200 and r.json()['event']['questions'][0]['key']=='use_type'
    assert client.get(p+'/owner/events').json()['model_generated'] is False


def test_default_http_policy_execution_is_unavailable(api):
    app,client,p=api
    assert client.get(p+'/policies').json()['execution_enabled'] is False
    r=client.post(p+'/posting-authorizations/nonexistent/execute',json={})
    assert r.status_code==403 and r.json()['code']=='POLICY_EXECUTION_NOT_RELEASED'


def test_release_progress_shows_ten_unaccepted_versions(api):
    app,client,p=api
    result=client.get(p+'/ops/release-readiness')
    assert result.status_code==200
    assert len(result.json()['versions'])==10 and result.json()['final_accepted'] is False
    assert result.json()['evidence_records_loaded']==0


def test_owner_cannot_submit_specialist_payroll_inputs(api):
    app,client,p=api
    assert client.get(p+'/submission-packages').json()['live_enabled'] is False
    report=client.get(p+'/owner/reports?start=2026-09-01&end=2026-09-30').json()
    assert report['cash']['closing_balance'] is None
    assert report['official_tax_ready'] is False


def test_late_source_upload_invalidates_existing_mock_filing_snapshot(w):
    engine,filing,period=w.ready_filing()
    upload(w,BANK)
    with pytest.raises(DomainError) as error:engine.submit(w.owner,w.company,filing['id'],SubmitInput(expected_revision=filing['revision']))
    assert error.value.code=='FILING_BASIS_CHANGED'
    with w.db.connect() as c:
        assert not w.db.list(c,w.company,'submission')


def test_all_new_reads_require_session(w):
    app=create_app(w.db.path);prefix='/api/companies/'+w.company
    with TestClient(app) as client:
        for suffix in ['/owner/events','/owner/questions','/policies','/expert-reviews','/submission-packages','/ops/release-readiness','/owner/reports?start=2026-01-01&end=2026-12-31']:
            assert client.get(prefix+suffix).status_code==401
