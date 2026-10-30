from __future__ import annotations
import hashlib
import pytest
from fastapi.testclient import TestClient
from price.api import create_app
from price.owner import CompanyFactsInput, OwnerView
from price.uploads import Uploads
from price.ingestion import Ingestion
from price.db import DomainError
from conftest import PASSWORD
from test_source_ingestion import BANK, INVOICES, upload


@pytest.fixture
def web(w):
    app = create_app(w.db.path)
    with TestClient(app) as client:
        login = client.post('/api/login', json={'username': w.owner.username, 'password': PASSWORD})
        assert login.status_code == 200
        client.headers['x-csrf-token'] = login.json()['csrf']
        yield app, client, '/api/companies/' + w.company


def start(client, path, raw=BANK, key='web-source-0001'):
    body = {'idempotency_key': key, 'files': [{'client_file_id':'f1','filename':'통장.csv','size_bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}]}
    r = client.post(path + '/uploads', json=body)
    assert r.status_code == 201, r.text
    return r.json()


def send(client, path, batch, raw=BANK, extra=None):
    return client.put(path + '/uploads/' + batch['id'] + '/files/f1/chunks/0', content=raw,
                      headers={'content-type':'application/octet-stream','x-chunk-sha256':hashlib.sha256(raw).hexdigest(), **(extra or {})})


def test_owner_default_and_expert_views_coexist(web):
    app, client, path = web
    assert '자료 올리기' in client.get('/').text
    assert 'owner.js' in client.get('/').text
    assert 'app.js' in client.get('/expert').text
    assert client.get('/static/owner.js').status_code == 200
    assert client.get('/static/owner.css').status_code == 200


def test_binary_upload_to_source_case_without_manual_transaction(web, w):
    app, client, path = web
    batch = start(client, path)
    assert send(client, path, batch).status_code == 200
    r = client.post(path + '/uploads/' + batch['id'] + '/complete', json={'expected_revision':batch['revision']})
    assert r.status_code == 200, r.text
    body = r.json()
    doc = body['files'][0]['document']['id']
    assert body['accounting_complete'] is False and body['manual_transaction_required'] is False
    assert body['source_reading'] and body['source_reading'][0]['cases'] == 2
    assert body['casework']['events'] >= 1
    assert app.state.source_reader.run_once(w.company)['processed'] is False
    result = client.get(path + '/documents/' + doc + '/extraction').json()
    assert result['total'] == 2
    home = client.get(path + '/owner/home').json()
    assert home['counts']['read'] == 1 and home['accounting_complete'] is False
    assert not client.get(path + '/transactions').json()
    assert not client.get(path + '/journals').json()


@pytest.mark.parametrize('headers,expected', [
    ({'x-csrf-token':'incorrect'},403),
    ({'origin':'https://other.invalid'},403),
    ({'sec-fetch-site':'cross-site'},403),
    ({'content-type':'text/plain'},415),
    ({'content-type':'application/octet-stream-not-real'},415),
    ({'x-chunk-sha256':'0'*64},422),
])
def test_chunk_keeps_all_request_boundaries(web, headers, expected):
    app, client, path = web; batch = start(client, path)
    result = send(client, path, batch, extra=headers)
    assert result.status_code == expected
    view = client.get(path + '/uploads/' + batch['id']).json()
    assert view['files'][0]['received_bytes'] == 0


def test_chunk_body_limit_measures_received_bytes(web):
    app, client, path = web; batch = start(client, path)
    assert send(client, path, batch, b'x' * 1_000_001).status_code == 413


def test_binary_exception_does_not_apply_to_other_api(web):
    app, client, path = web
    assert client.post(path + '/transactions', content=b'{}', headers={'content-type':'application/octet-stream'}).status_code == 415


def test_unauthenticated_binary_cannot_create_chunks(web):
    app, client, path = web; batch = start(client, path)
    with TestClient(app) as anonymous:
        assert send(anonymous, path, batch).status_code == 401


def test_posted_ledger_self_approval_boundary_unchanged(web, w):
    app, client, path = web
    tx = w.tx()
    j = w.analyze(tx)['journal']
    r = client.post(path + '/journals/' + j['id'] + '/approve', json={'expected_revision':j['revision'],'reason':'self approval must remain blocked'})
    assert r.status_code == 403 and r.json()['code'] == 'SELF_APPROVAL'


def test_factual_onboarding_not_tax_approval(web):
    app, client, path = web
    info = client.get(path + '/owner/onboarding').json()
    r = client.post(path + '/owner/onboarding', json={'expected_revision':info['revision'],'name':'시험 회사 A','mode':'company','business_number':'111-11-11111','confirmed':True,'instruments':[{'kind':'bank','identifier':'123-456-789012'}]})
    assert r.status_code == 200, r.text
    assert r.json()['verification'] == 'owner_factual_statement'
    assert r.json()['legal_registry_verified'] is False
    assert r.json()['instruments'][0]['currency'] is None
    assert client.get(path + '/owner/home').json()['needs_company_confirmation'] is False


def test_onboarding_mode_and_stale_version(web):
    app, client, path = web
    data = {'expected_revision':0,'name':'시험 회사 A','mode':'demo','confirmed':True}
    assert client.post(path+'/owner/onboarding',json=data).status_code == 409
    data['mode']='company'
    assert client.post(path+'/owner/onboarding',json=data).status_code == 200
    assert client.post(path+'/owner/onboarding',json=data).status_code == 409


def test_institution_support_is_not_invented(web):
    app, client, path = web
    support = client.get(path + '/support/formats').json()
    assert support['accepted_institution_formats'] == []
    assert all(x['verification'] == 'synthetic_only' for x in support['development_readers'])


def test_confirmed_wrong_company_account_is_not_promoted(w):
    uploads = Uploads(w.svc)
    owner = OwnerView(w.svc, uploads)
    owner.confirm(w.owner, w.company, CompanyFactsInput(expected_revision=0,name='시험 회사 A',mode='company',confirmed=True,
        instruments=[{'kind':'bank','identifier':'999-999-999999','currency':'KRW'}]))
    upload(w)
    result = Ingestion(w.svc).run_once(w.company)
    assert result['state'] == 'BLOCKED'
    assert result['error'] == 'COMPANY_SOURCE_CONFLICT'
    with w.db.connect() as c:
        assert not w.db.list(c, w.company, 'ingestion_case')


def test_invoice_both_parties_different_from_confirmed_company(w):
    uploads = Uploads(w.svc)
    owner = OwnerView(w.svc, uploads)
    owner.confirm(w.owner,w.company,CompanyFactsInput(expected_revision=0,name='시험 회사 A',mode='company',business_number='9999999999',confirmed=True))
    upload(w, INVOICES)
    result = Ingestion(w.svc).run_once(w.company)
    assert result['error'] == 'COMPANY_SOURCE_CONFLICT'

def _confirm_buyer(client, path):
    info = client.get(path + '/owner/onboarding').json()
    saved = client.post(path + '/owner/onboarding', json={'expected_revision': info['revision'], 'name': '시험 회사 A', 'mode': 'company', 'business_number': '222-22-22222', 'confirmed': True, 'instruments': [{'kind': 'bank', 'identifier': '123-456-789012'}]})
    assert saved.status_code == 200, saved.text


def test_complete_returns_dynamic_questions_and_skips_older_queue(web, w):
    app, client, path = web
    _confirm_buyer(client, path)
    for index in range(100):
        raw = ('합성 검증용 거래내역\n계좌번호: 123-456-789012\n통화: KRW\n'
               '거래일시,거래내용,입금액,출금액,거래후잔액\n'
               f'2026-08-01 10:00:00,대기{index},{index + 1},0,{index + 1}\n').encode()
        upload(w, raw, filename=f'대기{index}.csv', key=f'queued-bank-{index:03d}')
    batch = start(client, path, INVOICES, key='web-question-invoice')
    assert send(client, path, batch, INVOICES).status_code == 200
    done = client.post(path + '/uploads/' + batch['id'] + '/complete', json={'expected_revision': batch['revision']})
    assert done.status_code == 200, done.text
    body = done.json()
    document_id = body['files'][0]['document']['id']
    assert body['reading_pending'] is False and body['reading_attention'] == []
    assert body['source_reading'] and {step['document_id'] for step in body['source_reading']} == {document_id}
    question = body['owner_questions'][0]
    assert question['key'] == 'business_use'
    assert set(question['choices']) == {'business', 'personal', 'unknown'}
    listed = client.get(path + '/owner/questions').json()['questions']
    same = next(item for item in listed if item['event_id'] == question['event_id'])
    assert same['key'] == question['key'] and set(same['choices']) == set(question['choices'])
    answered = client.post(path + '/events/' + question['event_id'] + '/facts', json={
        'expected_revision': question['expected_revision'],
        'context_hash': question['context_hash'],
        'question': question['key'],
        'answer': 'business',
        'idempotency_key': 'fact-business-use-001',
    })
    assert answered.status_code == 200, answered.text
    saved = answered.json()
    assert saved['saved'] is True and saved['event']['facts']['business_use'] == 'business'
    assert saved['event']['accounting_complete'] is False
    assert [item['key'] for item in saved['event']['questions']] == ['use_type']
    home = client.get(path + '/owner/home').json()
    assert home['accounting_complete'] is False
    assert not client.get(path + '/transactions').json()
    assert not client.get(path + '/journals').json()
    with w.db.connect() as c:
        assert c.execute("SELECT count(*) FROM upload_jobs WHERE company_id=? AND state='QUEUED'", (w.company,)).fetchone()[0] == 100
        stored = w.db.get(c, w.company, 'economic_event', question['event_id'])
        assert 'questions' not in stored


def test_live_lease_keeps_batch_pending_instead_of_claiming_completion(web, w):
    app, client, path = web
    _confirm_buyer(client, path)
    batch = start(client, path, INVOICES, key='web-leased-invoice')
    assert send(client, path, batch, INVOICES).status_code == 200
    sealed = Uploads(w.svc).complete(w.owner, w.company, batch['id'], batch['revision'])
    document_id = sealed['files'][0]['document']['id']
    assert Ingestion(w.svc).claim(w.company, document_id)
    done = client.post(path + '/uploads/' + batch['id'] + '/complete', json={'expected_revision': sealed['revision']})
    assert done.status_code == 200, done.text
    body = done.json()
    assert body['reading_pending'] is True
    assert body['owner_questions'] == []
    assert body['source_reading'] == []
    assert body['accounting_complete'] is False
    document = client.get(path + '/documents/' + document_id).json()
    assert document['state'] == 'PROCESSING'
    assert body['reading_attention'] == []
    assert client.get(path + '/owner/questions').json()['questions'] == []
    assert client.get(path + '/owner/events').json()['accounting_complete'] is False
    assert not client.get(path + '/transactions').json()
    assert not client.get(path + '/journals').json()


def test_unreadable_batch_reports_attention_without_a_fact_question(web):
    app, client, path = web
    raw = b'unknown,a,b\n1,2,3\n'
    batch = start(client, path, raw, key='web-attention-unknown')
    assert send(client, path, batch, raw).status_code == 200
    done = client.post(path + '/uploads/' + batch['id'] + '/complete', json={'expected_revision': batch['revision']})
    assert done.status_code == 200, done.text
    body = done.json()
    document_id = body['files'][0]['document']['id']
    assert body['reading_pending'] is False and body['owner_questions'] == []
    assert body['accounting_complete'] is False and body['manual_transaction_required'] is False
    assert body['reading_attention'] and body['reading_attention'][0]['document_id'] == document_id
    assert body['reading_attention'][0]['state'] not in ('NORMALIZED', 'QUEUED', 'PROCESSING')
    assert client.get(path + '/owner/questions').json()['questions'] == []
    assert not client.get(path + '/journals').json()

def test_owner_reopens_onboarding_to_confirm_discovered_account(web):
    app, client, path = web
    first = client.post(path + '/owner/onboarding', json={'expected_revision': 0, 'name': '시험 회사 A', 'mode': 'company', 'business_number': '111-11-11111', 'confirmed': True, 'instruments': []})
    assert first.status_code == 200, first.text
    saved = first.json()
    batch = start(client, path, BANK, key='web-account-candidate')
    assert send(client, path, batch, BANK).status_code == 200
    done = client.post(path + '/uploads/' + batch['id'] + '/complete', json={'expected_revision': batch['revision']})
    assert done.status_code == 200, done.text
    info = client.get(path + '/owner/onboarding').json()
    assert info['revision'] == saved['revision']
    assert info['facts']['name'] == saved['name'] and info['facts']['business_number'] == saved['business_number']
    assert info['facts']['instruments'] == []
    candidate = next(item for item in info['instrument_candidates'] if item['kind'] == 'bank')
    assert candidate['ownership_verified'] is False and candidate['identifier'] == '123456789012'
    home = client.get(path + '/owner/home').json()
    assert home['needs_company_confirmation'] is False
    assert home['pending_instrument_candidates'][0]['identifier'] == candidate['identifier']
    assert home['owner_actions'][0]['kind'] == 'confirm_instruments'
    screen = client.get('/static/owner.js').text + client.get('/static/book_results.js').text
    assert 'pending_instrument_candidates' in screen and 'showOnboarding' in screen
    skipped = client.post(path + '/owner/onboarding', json={'expected_revision': info['revision'], 'name': info['facts']['name'], 'mode': info['mode'], 'business_number': info['facts']['business_number'], 'confirmed': True, 'instruments': []})
    assert skipped.status_code == 200 and skipped.json()['instruments'] == []
    confirmed = client.post(path + '/owner/onboarding', json={'expected_revision': skipped.json()['revision'], 'name': info['facts']['name'], 'mode': info['mode'], 'business_number': info['facts']['business_number'], 'confirmed': True, 'instruments': [{'kind': 'bank', 'identifier': candidate['identifier'], 'currency': candidate['currency']}]})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()['name'] == saved['name'] and confirmed.json()['business_number'] == saved['business_number']
    assert confirmed.json()['instruments'] == [{'kind': 'bank', 'identifier': candidate['identifier'], 'currency': candidate['currency']}]
    assert client.get(path + '/owner/home').json()['pending_instrument_candidates'] == []
    assert not client.get(path + '/journals').json()

def test_company_fact_save_resumes_questions_without_worker(web):
    app, client, path = web
    first = client.post(path + '/owner/onboarding', json={'expected_revision': 0, 'name': '시험 회사 A', 'mode': 'company', 'business_number': '111-11-11111', 'confirmed': True, 'instruments': []})
    assert first.status_code == 200, first.text
    batch = start(client, path, BANK, key='web-fact-resume')
    assert send(client, path, batch, BANK).status_code == 200
    done = client.post(path + '/uploads/' + batch['id'] + '/complete', json={'expected_revision': batch['revision']})
    assert done.status_code == 200, done.text
    document_id = done.json()['files'][0]['document']['id']
    before = client.get(path + '/owner/questions').json()
    assert before['questions'] == [] and before['source_changed'] == 0
    info = client.get(path + '/owner/onboarding').json()
    candidate = next(item for item in info['instrument_candidates'] if item['kind'] == 'bank')
    saved = client.post(path + '/owner/onboarding', json={'expected_revision': info['revision'], 'name': info['facts']['name'], 'mode': info['mode'], 'business_number': info['facts']['business_number'], 'confirmed': True, 'instruments': [{'kind': 'bank', 'identifier': candidate['identifier'], 'currency': candidate['currency']}]})
    assert saved.status_code == 200, saved.text
    assert saved.json()['revision'] == info['revision'] + 1
    assert saved.json()['casework_resume']['state'] == 'ready'
    assert saved.json()['name'] == '시험 회사 A' and saved.json()['business_number'] == '1111111111'
    questions = client.get(path + '/owner/questions').json()
    events = client.get(path + '/owner/events').json()['events']
    assert questions['source_changed'] == 0 and questions['questions']
    assert {item['key'] for item in questions['questions']} == {'cash_reason'}
    assert questions['questions'][0]['context_hash'] == next(event['context_hash'] for event in events if event['id'] == questions['questions'][0]['event_id'])
    assert all(event['state'] != 'SOURCE_CHANGED' for event in events)
    answer = questions['questions'][0]
    posted = client.post(path + '/events/' + answer['event_id'] + '/facts', json={'expected_revision': answer['expected_revision'], 'context_hash': answer['context_hash'], 'question': 'cash_reason', 'answer': 'customer_payment', 'idempotency_key': 'fact-resume-01'})
    assert posted.status_code == 200, posted.text
    again = client.get(path + '/owner/onboarding').json()
    kept = client.post(path + '/owner/onboarding', json={'expected_revision': again['revision'], 'name': again['facts']['name'], 'mode': again['mode'], 'business_number': again['facts']['business_number'], 'confirmed': True, 'instruments': again['facts']['instruments']})
    assert kept.status_code == 200, kept.text
    resumed = client.get(path + '/owner/events').json()['events']
    same = next(event for event in resumed if event['id'] == answer['event_id'])
    assert same['state'] != 'SOURCE_CHANGED' and same['facts'].get('cash_reason') == 'customer_payment'
    assert client.get(path + '/documents/' + document_id).json()['state'] == 'NORMALIZED'
    assert not client.get(path + '/journals').json()
