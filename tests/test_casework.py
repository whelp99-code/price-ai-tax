from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import hashlib
import shutil
from fastapi.testclient import TestClient
from price.api import create_app
from price.db import Database
from price.service import Service
from price.uploads import Uploads
from price.upload_models import UploadBatchInput
from conftest import PASSWORD, World
import pytest
from pydantic import ValidationError
from price.casework import Casework, FactAnswer
from price.owner import OwnerView, CompanyFactsInput, DocumentContextInput
from price.ingestion import Ingestion
from price.extractions import Extractions,ReprocessInput
from price.db import DomainError
from test_source_ingestion import upload,BANK,INVOICES,CARD


def setup_sources(w,number='2222222222',documents=None):
    reader=Ingestion(w.svc)
    OwnerView(w.svc,reader.uploads).confirm(w.owner,w.company,CompanyFactsInput(expected_revision=0,name='시험 회사 A',business_number=number,mode='company',confirmed=True,instruments=[{'kind':'bank','identifier':'123456789012','currency':'KRW'},{'kind':'card','identifier':'1111********1111','currency':'KRW'}]))
    for i,raw in enumerate(documents or [BANK,INVOICES,CARD]):upload(w,raw,key=f'casework-source-{i}')
    while reader.run_once(w.company)['processed']:pass
    work=Casework(w.svc);work.refresh(w.owner,w.company)
    return work


def answer(work,w,event,question,value,key='answer-key-0001',expected_fact_revision=0):
    return work.answer(w.owner,w.company,event['id'],FactAnswer(expected_revision=event['revision'],expected_fact_revision=expected_fact_revision,context_hash=event['context_hash'],question=question,answer=value,idempotency_key=key))


def invoice(work,w):
    return next(x for x in work.list(w.owner,w.company) if x['kind']=='invoice')


def test_sources_become_economic_events_with_fact_questions(w):
    work=setup_sources(w)
    events=work.list(w.owner,w.company)
    assert len(events)==5
    purchase=invoice(work,w)
    assert purchase['direction']=='purchase' and purchase['ownership']=='owner_confirmed'
    assert purchase['questions'][0]['key']=='business_use'
    assert all(not e['accounting_complete'] for e in events)
    with w.db.connect() as c:assert not w.db.list(c,w.company,'journal')


def test_factual_answer_resumes_next_question_without_tax_input(w):
    work=setup_sources(w);event=invoice(work,w)
    result=answer(work,w,event,'business_use','business')
    assert result['event']['questions'][0]['key']=='use_type'
    result=answer(work,w,event,'use_type','operations',key='answer-key-0002')
    assert result['event']['state']=='FACTS_READY_ACCOUNTING_REVIEW'
    assert result['event']['questions']==[]
    assert result['event']['facts']=={'business_use':'business','use_type':'operations'}
    assert w.owner.id in result['event']['fact_actor_ids']


def test_idempotent_answer_same_actor_and_body(w):
    work=setup_sources(w);event=invoice(work,w)
    a=answer(work,w,event,'business_use','business')
    b=answer(work,w,event,'business_use','business')
    assert a['fact_id']==b['fact_id'] and b['reused']
    with pytest.raises(DomainError):answer(work,w,event,'business_use','personal')


def test_unknown_routes_to_real_connection_needed_not_fake_review(w):
    work=setup_sources(w);event=invoice(work,w)
    result=answer(work,w,event,'business_use','unknown')
    assert result['event']['state']=='REVIEW_CONNECTION_NEEDED'
    assert result['event']['expert_assignee'] is None
    assert not result['event']['questions']


def test_fact_correction_retires_old_dependent_purpose(w):
    work=setup_sources(w);event=invoice(work,w)
    answer(work,w,event,'business_use','business')
    answer(work,w,event,'use_type','operations',key='answer-key-purpose')
    changed=answer(work,w,event,'business_use','personal',key='answer-key-changed',expected_fact_revision=1)
    assert changed['event']['facts']=={'business_use':'personal'}
    with w.db.connect() as c:
        assert c.execute("SELECT count(*) FROM object_history WHERE kind='event_fact'").fetchone()[0]>=1


def test_other_company_and_viewer_cannot_supply_facts(w):
    work=setup_sources(w);event=invoice(work,w)
    data=FactAnswer(expected_revision=event['revision'],context_hash=event['context_hash'],question='business_use',answer='business',idempotency_key='request-other-user')
    for actor in (w.other_owner,w.viewer,w.reviewer):
        with pytest.raises(DomainError):work.answer(actor,w.company,event['id'],data)
    with pytest.raises(DomainError):work.list(w.other_owner,w.company)


def test_client_cannot_inject_account_tax_or_approval(w):
    work=setup_sources(w);event=invoice(work,w)
    with pytest.raises(ValidationError):
        FactAnswer(expected_revision=event['revision'],context_hash=event['context_hash'],question='deductible',answer='yes',idempotency_key='request-extra',approved=True)
    with pytest.raises(DomainError):answer(work,w,event,'business_use','deductible')


def test_source_reprocessing_invalidates_old_question_before_worker_runs(w):
    work=setup_sources(w);event=invoice(work,w)
    ext=Extractions(w.svc);doc=ext.uploads.document(w.owner,w.company,event['document_ids'][0])
    ext.reprocess(w.owner,w.company,doc['id'],ReprocessInput(expected_revision=doc['revision'],idempotency_key='reparse-question-01',reason='source parser revalidation'))
    with pytest.raises(DomainError) as failure:answer(work,w,event,'business_use','business')
    assert failure.value.code=='SOURCE_CHANGED'
    assert next(x for x in work.list(w.owner,w.company) if x['id']==event['id'])['state']=='SOURCE_CHANGED'


def test_current_sources_restore_after_reprocess_without_duplicate_events(w):
    work=setup_sources(w);event=invoice(work,w);reader=Ingestion(w.svc)
    doc=reader.uploads.document(w.owner,w.company,event['document_ids'][0])
    Extractions(w.svc).reprocess(w.owner,w.company,doc['id'],ReprocessInput(expected_revision=doc['revision'],idempotency_key='reparse-event-01',reason='parser revalidation'))
    reader.run_once(w.company);work.refresh(w.owner,w.company)
    events=[e for e in work.list(w.owner,w.company) if e['kind']=='invoice']
    assert len(events)==1 and events[0]['id']==event['id']
    assert events[0]['context_hash']!=event['context_hash']


def test_strong_invoice_identity_merges_two_sources_not_two_purchases(w):
    alt=INVOICES.replace(b',1000,100,1100',b',1000.00,100.00,1100.00')
    work=setup_sources(w,documents=[INVOICES,alt])
    events=work.list(w.owner,w.company)
    assert len(events)==1 and len(events[0]['source_record_ids'])==2
    assert 'SOURCE_IDENTITY_CONFLICT' not in events[0]['issues']


def test_same_invoice_reference_changed_money_is_conflict_not_silent_update(w):
    changed=INVOICES.replace(b',1000,100,1100',b',2000,200,2200')
    work=setup_sources(w,documents=[INVOICES,changed])
    event=work.list(w.owner,w.company)[0]
    assert 'SOURCE_IDENTITY_CONFLICT' in event['issues']
    assert event['state']=='REVIEW_CONNECTION_NEEDED'
    assert event['questions']==[]


def test_normal_same_amount_bank_rows_without_ids_are_not_deleted(w):
    raw=('계좌번호: 123456789012\n통화: KRW\n거래일자,거래내용,입금액,출금액\n2026-09-01,거래,100,0\n2026-09-01,거래,100,0\n').encode()
    work=setup_sources(w,documents=[raw]);assert len(work.list(w.owner,w.company))==2


def test_same_amount_only_proposes_never_allocates(w):
    work=setup_sources(w,number='1111111111',documents=[BANK,INVOICES]);event=invoice(work,w)
    result=work.candidates(w.owner,w.company,event['id'])
    assert len(result['candidates'])==1
    assert result['candidates'][0]['automatic_allocation'] is False
    with w.db.connect() as c:assert not w.db.list(c,w.company,'allocation')


def test_concurrent_refresh_one_economic_view(w):
    work=setup_sources(w)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:work.refresh(w.owner,w.company),range(2)))
    assert not any(r['updated'] for r in results)
    assert len(work.list(w.owner,w.company))==5


def test_concurrent_fact_answers_require_current_fact_revision(w):
    work=setup_sources(w);event=invoice(work,w)
    def attempt(choice):
        try:
            answer(work,w,event,'business_use',choice,key='fact-concurrent-'+choice)
            return 'saved'
        except DomainError as error:return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,['business','personal']))
    assert sorted(results)==['FACT_STALE_VERSION','saved']


def test_event_list_reads_current_source_snapshot_only_once(w,monkeypatch):
    work=setup_sources(w)
    original=work._current
    calls=[]
    def counted(c,company):
        calls.append(company)
        return original(c,company)
    monkeypatch.setattr(work,'_current',counted)
    events=work.list(w.owner,w.company)
    assert len(events)==5 and calls==[w.company]

BARE = ('거래일시,거래내용,입금액,출금액,거래후잔액,거래번호\n'
        '2026-09-03 10:00:00,용역대금,1100,0,1100,SYN-BARE-1\n').encode()
FRACTION_BARE = BARE.replace(b'1100,0,1100', b'1.25,0,1.25')
MASKED_ACCOUNT = ('거래일시,거래내용,입금액,출금액,계좌번호,거래번호\n'
                  '2026-09-03 10:00:00,용역대금,1100,0,1234****9012,SYN-MASK-1\n').encode()
OTHER_ACCOUNT = ('거래일시,거래내용,입금액,출금액,계좌번호,거래번호\n'
                 '2026-09-03 10:00:00,용역대금,1100,0,999888777666,SYN-OTHER-1\n').encode()


def facts(w, reader, instruments=None):
    return OwnerView(w.svc, reader.uploads).confirm(w.owner, w.company, CompanyFactsInput(
        expected_revision=0, name='시험 회사 A', business_number='2222222222', mode='company', confirmed=True,
        instruments=instruments if instruments is not None else [{'kind': 'bank', 'identifier': '123456789012', 'currency': 'KRW'}]))


def read_document(w, raw, key):
    reader = Ingestion(w.svc)
    batch = upload(w, raw, key=key)
    while reader.run_once(w.company)['processed']:
        pass
    document = reader.uploads.document(w.owner, w.company, batch['files'][0]['document']['id'])
    return reader, document


def confirm_context(w, reader, document, currency='KRW', identifier='123456789012', key='doc-context-0001', expected_context_revision=0, generation=None, source_hash=None, expected_revision=None):
    data = DocumentContextInput(
        expected_revision=document['revision'] if expected_revision is None else expected_revision,
        expected_context_revision=expected_context_revision,
        source_hash=document['sha256'] if source_hash is None else source_hash,
        generation=document['result']['generation'] if generation is None else generation,
        identifier=identifier, currency=currency, confirmed=True, idempotency_key=key)
    return OwnerView(w.svc, reader.uploads).confirm_document_context(w.owner, w.company, document['id'], data)


def test_metadata_complete_bank_keeps_source_currency_and_questions(w):
    work = setup_sources(w)
    banks = [event for event in work.list(w.owner, w.company) if event['kind'] == 'bank']
    assert banks and all(event['values']['currency'] == 'KRW' and event['context_origin'] == 'source' for event in banks)
    assert all(event['questions'] and event['questions'][0]['key'] == 'cash_reason' for event in banks)
    assert all(not event['accounting_complete'] for event in banks)
    with w.db.connect() as c:
        assert not w.db.list(c, w.company, 'document_source_context')
        assert not w.db.list(c, w.company, 'journal')


def test_missing_context_stays_unresolved_until_document_confirmation(w):
    reader = Ingestion(w.svc)
    saved = facts(w, reader)
    assert saved['instruments'][0]['currency'] == 'KRW'
    _reader, document = read_document(w, BARE, 'bare-bank-1')
    work = Casework(w.svc)
    work.refresh(w.owner, w.company)
    event = work.list(w.owner, w.company)[0]
    assert event['issues'] == ['CURRENCY_UNCONFIRMED', 'OWNERSHIP_UNCONFIRMED']
    assert not event['values']['currency'] and not event['values']['account']
    assert event['questions'] == [] and event['state'] == 'REVIEW_CONNECTION_NEEDED'
    assert event['accounting_complete'] is False
    home = OwnerView(w.svc, reader.uploads).home(w.owner, w.company)
    action = next(item for item in home['owner_actions'] if item['kind'] == 'confirm_document_context')
    assert action['document_id'] == document['id'] and action['candidates'][0]['currency'] is None
    assert action['candidates'][0]['instrument_currency_applied'] is False
    assert action['refresh_skipped_evidence'] is False and home['initial_ingestion_waiting_means_refresh_skipped'] is False
    rows = reader.records(w.owner, w.company, document['id'])['rows']
    assert rows[0]['values']['currency'] is None and rows[0]['values']['account'] is None
    with w.db.connect() as c:
        assert c.execute("SELECT json_extract(body,'$.state') AS state FROM objects WHERE company_id=? AND kind='ingestion_case'", (w.company,)).fetchone()['state'] == 'WAITING_FOR_LINKING'
        assert c.execute('SELECT content FROM blobs WHERE company_id=? AND sha256=?', (w.company, document['sha256'])).fetchone()['content'] == BARE
    before_ids = [item['id'] for item in work.list(w.owner, w.company)]
    confirmed = confirm_context(w, reader, document)
    assert confirmed['currency_origin'] == 'owner_statement' and confirmed['instrument_currency_applied'] is False
    assert confirmed['applied_to_original'] is False and confirmed['casework_resume']['state'] == 'ready'
    resumed = work.list(w.owner, w.company)
    assert [item['id'] for item in resumed] == before_ids
    event = resumed[0]
    assert event['questions'][0]['key'] == 'cash_reason' and event['issues'] == []
    assert event['values']['currency'] == 'KRW' and event['values']['account'] == '123456789012'
    assert event['context_origin'] == 'owner_factual_statement' and event['applied_context'][0]['currency_origin'] == 'owner_statement'
    assert event['accounting_complete'] is False
    rows = reader.records(w.owner, w.company, document['id'])['rows']
    assert rows[0]['values']['currency'] is None and rows[0]['values']['account'] is None
    with w.db.connect() as c:
        assert not w.db.list(c, w.company, 'journal')
        assert any(row['action'] == 'document.context_confirmed' for row in c.execute('SELECT action FROM audit WHERE company_id=?', (w.company,)))

def test_later_krw_confirmation_rejects_fractional_won_without_changing_source(w):
    reader = Ingestion(w.svc)
    facts(w, reader)
    _reader, document = read_document(w, FRACTION_BARE, 'fraction-bare-1')
    assert document['state'] == 'NORMALIZED'
    work = Casework(w.svc)
    work.refresh(w.owner, w.company)
    before = work.list(w.owner, w.company)[0]
    assert before['questions'] == [] and 'CURRENCY_UNCONFIRMED' in before['issues']
    with pytest.raises(DomainError) as error:
        confirm_context(w, reader, document, key='doc-context-fraction')
    assert error.value.code == 'KRW_FRACTION_REVIEW'
    event = work.list(w.owner, w.company)[0]
    assert event['questions'] == [] and event['values']['currency'] is None
    assert event['values']['gross'] == '1.25'
    rows = reader.records(w.owner, w.company, document['id'])['rows']
    assert rows[0]['values']['currency'] is None and rows[0]['values']['gross'] == '1.25'
    with w.db.connect() as c:
        assert not w.db.list(c, w.company, 'document_source_context')
        assert not w.db.list(c, w.company, 'journal')
        assert c.execute('SELECT content FROM blobs WHERE company_id=? AND sha256=?', (w.company, document['sha256'])).fetchone()['content'] == FRACTION_BARE

def test_later_non_krw_confirmation_keeps_fractional_amount_and_resumes_questions(w):
    reader = Ingestion(w.svc)
    facts(w, reader)
    _reader, document = read_document(w, FRACTION_BARE, 'fraction-usd-1')
    work = Casework(w.svc)
    work.refresh(w.owner, w.company)
    confirmed = confirm_context(w, reader, document, currency='USD', key='doc-context-fraction-usd')
    assert confirmed['currency'] == 'USD' and confirmed['applied_to_original'] is False
    event = work.list(w.owner, w.company)[0]
    assert event['issues'] == [] and event['questions'][0]['key'] == 'cash_reason'
    assert event['values']['currency'] == 'USD' and event['values']['gross'] == '1.25'
    rows = reader.records(w.owner, w.company, document['id'])['rows']
    assert rows[0]['values']['currency'] is None and rows[0]['values']['gross'] == '1.25'


def test_document_context_rejects_cross_company_viewer_and_stale_bindings(w):
    reader = Ingestion(w.svc)
    facts(w, reader)
    _reader, document = read_document(w, BARE, 'bare-bank-bounds')
    for actor in (w.viewer, w.preparer, w.reviewer, w.other_owner):
        with pytest.raises(DomainError) as error:
            OwnerView(w.svc, reader.uploads).confirm_document_context(actor, w.company, document['id'], DocumentContextInput(
                expected_revision=document['revision'], source_hash=document['sha256'], generation=document['result']['generation'],
                identifier='123456789012', currency='KRW', confirmed=True, idempotency_key='doc-context-denied'))
        assert error.value.code in ('FORBIDDEN', 'NOT_FOUND')
    with pytest.raises(DomainError) as error:
        confirm_context(w, reader, document, key='doc-context-stale-rev', expected_revision=document['revision'] + 1)
    assert error.value.code == 'STALE_VERSION'
    with pytest.raises(DomainError) as error:
        confirm_context(w, reader, document, key='doc-context-stale-hash', source_hash='a' * 64)
    assert error.value.code == 'STALE_SOURCE'
    with pytest.raises(DomainError) as error:
        confirm_context(w, reader, document, key='doc-context-stale-gen', generation=document['result']['generation'] + 1)
    assert error.value.code == 'STALE_GENERATION'
    with pytest.raises(DomainError) as error:
        confirm_context(w, reader, document, key='doc-context-masked', identifier='1234****9012')
    assert error.value.code == 'DOCUMENT_CONTEXT_MASKED'
    with pytest.raises(DomainError) as error:
        OwnerView(w.svc, reader.uploads).confirm_document_context(w.owner, w.other_company, document['id'], DocumentContextInput(
            expected_revision=document['revision'], source_hash=document['sha256'], generation=1,
            identifier='123456789012', currency='KRW', confirmed=True, idempotency_key='doc-context-other-co'))
    assert error.value.code == 'NOT_FOUND'


def test_masked_or_conflicting_source_identity_cannot_be_bypassed(w):
    reader = Ingestion(w.svc)
    facts(w, reader)
    _reader, masked = read_document(w, MASKED_ACCOUNT, 'masked-account')
    assert masked['state'] == 'NORMALIZED', masked['state']
    with pytest.raises(DomainError) as error:
        confirm_context(w, reader, masked, key='doc-context-masked-source')
    assert error.value.code == 'DOCUMENT_CONTEXT_CONFLICT' and 'masked_source_account' in error.value.details
    _reader, other = read_document(w, OTHER_ACCOUNT, 'other-account')
    assert other['state'] == 'COMPANY_REVIEW'
    with pytest.raises(DomainError) as error:
        confirm_context(w, reader, other, key='doc-context-conflict')
    assert error.value.code == 'READER_NOT_READY'
    work = Casework(w.svc)
    work.refresh(w.owner, w.company)
    events = work.list(w.owner, w.company)
    assert all(event['ownership'] != 'owner_confirmed' for event in events)
    assert all(event['values']['currency'] is None for event in events)
    assert all(event['accounting_complete'] is False for event in events)


def test_context_change_and_reprocess_keep_provenance_without_duplicate_events(w):
    reader = Ingestion(w.svc)
    facts(w, reader)
    _reader, document = read_document(w, BARE, 'bare-bank-change')
    first = confirm_context(w, reader, document)
    work = Casework(w.svc)
    event = work.list(w.owner, w.company)[0]
    answer(work, w, event, 'cash_reason', 'customer_payment', key='bare-answer-0001')
    changed = confirm_context(w, reader, document, currency='USD', key='doc-context-0002', expected_context_revision=first['revision'])
    assert changed['reused'] is False and changed['currency'] == 'USD'
    resumed = work.list(w.owner, w.company)
    assert len(resumed) == 1 and resumed[0]['id'] == event['id']
    assert resumed[0]['facts'] == {} and resumed[0]['values']['currency'] == 'USD'
    assert resumed[0]['questions'][0]['key'] == 'cash_reason'
    assert resumed[0]['accounting_complete'] is False
    rows = reader.records(w.owner, w.company, document['id'])['rows']
    assert rows[0]['values']['currency'] is None
    with w.db.connect() as c:
        assert c.execute("SELECT count(*) FROM object_history WHERE company_id=? AND kind='document_source_context'", (w.company,)).fetchone()[0] == 1
        assert not w.db.list(c, w.company, 'journal')
        current = sum(item.get('lifecycle') == 'CURRENT' for item in w.db.list(c, w.company, 'economic_event'))
    assert current == 1
    fresh = reader.uploads.document(w.owner, w.company, document['id'])
    Extractions(w.svc).reprocess(w.owner, w.company, document['id'], ReprocessInput(expected_revision=fresh['revision'], idempotency_key='reprocess-context-1', reason='합성 재처리'))
    assert reader.run_once(w.company)['committed'] is True
    work.refresh(w.owner, w.company)
    with w.db.connect() as c:
        stored = w.db.list(c, w.company, 'document_source_context')[0]
        assert stored['active'] is False and stored['invalidation_reason'] == 'source_generation'
        assert any(row['action'] == 'document.context_invalidated' for row in c.execute('SELECT action FROM audit WHERE company_id=?', (w.company,)))
        assert sum(item.get('lifecycle') == 'CURRENT' for item in w.db.list(c, w.company, 'economic_event')) == 1
        assert not w.db.list(c, w.company, 'journal')
    again = work.list(w.owner, w.company)[0]
    assert again['facts'] == {} and 'CURRENCY_UNCONFIRMED' in again['issues']
    assert again['accounting_complete'] is False


def test_removing_confirmed_instrument_invalidates_document_context(w):
    reader = Ingestion(w.svc)
    saved = facts(w, reader)
    _reader, document = read_document(w, BARE, 'bare-bank-remove')
    confirm_context(w, reader, document, key='doc-context-remove')
    work = Casework(w.svc)
    event = work.list(w.owner, w.company)[0]
    answer(work, w, event, 'cash_reason', 'customer_payment', key='bare-answer-remove')
    OwnerView(w.svc, reader.uploads).confirm(w.owner, w.company, CompanyFactsInput(
        expected_revision=saved['revision'], name='시험 회사 A', business_number='2222222222', mode='company', confirmed=True, instruments=[]))
    resumed = work.list(w.owner, w.company)[0]
    assert resumed['facts'] == {} and 'CURRENCY_UNCONFIRMED' in resumed['issues'] and 'OWNERSHIP_UNCONFIRMED' in resumed['issues']
    with w.db.connect() as c:
        assert w.db.list(c, w.company, 'document_source_context')[0]['active'] is False
        assert not w.db.list(c, w.company, 'journal')

def test_first_use_without_confirmed_account_reaches_existing_questions(w):
    reader = Ingestion(w.svc)
    owner = OwnerView(w.svc, reader.uploads)
    named = owner.confirm(w.owner, w.company, CompanyFactsInput(
        expected_revision=0, name='시험 회사 A', business_number='2222222222', mode='company', confirmed=True, instruments=[]))
    assert named['instruments'] == []
    _reader, document = read_document(w, BARE, 'first-use-bare')
    home = owner.home(w.owner, w.company)
    assert home['pending_instrument_candidates'] == [] and home['accounting_complete'] is False
    assert any(item['kind'] == 'confirm_owner_account' for item in home['owner_actions'])
    action = next(item for item in home['owner_actions'] if item['kind'] == 'confirm_document_context')
    assert action['candidates'] == [] and action['document_id'] == document['id']
    work = Casework(w.svc)
    work.refresh(w.owner, w.company)
    before = work.list(w.owner, w.company)[0]
    assert before['questions'] == [] and before['accounting_complete'] is False
    assert 'CURRENCY_UNCONFIRMED' in before['issues'] and 'OWNERSHIP_UNCONFIRMED' in before['issues']
    for actor in (w.viewer, w.other_owner):
        with pytest.raises(DomainError) as error:
            owner.confirm(actor, w.company, CompanyFactsInput(
                expected_revision=named['revision'], name='시험 회사 A', business_number='2222222222', mode='company', confirmed=True,
                instruments=[{'kind': 'bank', 'identifier': '123456789012', 'currency': 'KRW'}]))
        assert error.value.code in ('FORBIDDEN', 'NOT_FOUND')
    with pytest.raises(DomainError) as error:
        confirm_context(w, reader, document, identifier='1234****9012', key='first-use-masked')
    assert error.value.code == 'DOCUMENT_CONTEXT_MASKED'
    saved = owner.confirm(w.owner, w.company, CompanyFactsInput(
        expected_revision=named['revision'], name='시험 회사 A', business_number='2222222222', mode='company', confirmed=True,
        instruments=[{'kind': 'bank', 'identifier': '123456789012', 'currency': 'KRW'}]))
    assert saved['instruments'] == [{'kind': 'bank', 'identifier': '123456789012', 'currency': 'KRW'}]
    assert saved['verification'] == 'owner_factual_statement'
    rows = reader.records(w.owner, w.company, document['id'])['rows']
    assert rows[0]['values']['account'] is None and rows[0]['values']['currency'] is None
    with w.db.connect() as c:
        assert not w.db.list(c, w.company, 'document_source_context')
        assert not w.db.list(c, w.company, 'journal')
        assert c.execute('SELECT content FROM blobs WHERE company_id=? AND sha256=?', (w.company, document['sha256'])).fetchone()['content'] == BARE
    held = work.list(w.owner, w.company)[0]
    assert held['questions'] == [] and held['accounting_complete'] is False
    home = owner.home(w.owner, w.company)
    assert all(item['kind'] != 'confirm_owner_account' for item in home['owner_actions'])
    action = next(item for item in home['owner_actions'] if item['kind'] == 'confirm_document_context')
    assert action['candidates'][0]['identifier'] == '123456789012' and action['candidates'][0]['currency'] is None
    assert action['candidates'][0]['instrument_currency_applied'] is False
    document = reader.uploads.document(w.owner, w.company, document['id'])
    with pytest.raises(DomainError) as error:
        confirm_context(w, reader, document, identifier='555555555555', key='first-use-other-account')
    assert error.value.code == 'DOCUMENT_CONTEXT_NOT_CANDIDATE'
    with pytest.raises(DomainError) as error:
        confirm_context(w, reader, document, key='first-use-stale-hash', source_hash='a' * 64)
    assert error.value.code == 'STALE_SOURCE'
    confirmed = confirm_context(w, reader, document, key='first-use-doc')
    assert confirmed['applied_to_original'] is False and confirmed['instrument_currency_applied'] is False
    assert confirmed['casework_resume']['state'] == 'ready'
    event = work.list(w.owner, w.company)[0]
    assert event['questions'][0]['key'] == 'cash_reason' and event['issues'] == []
    assert event['values']['account'] == '123456789012' and event['values']['currency'] == 'KRW'
    assert event['accounting_complete'] is False
    rows = reader.records(w.owner, w.company, document['id'])['rows']
    assert rows[0]['values']['account'] is None and rows[0]['values']['currency'] is None
    _reader, other = read_document(w, OTHER_ACCOUNT, 'first-use-other-source')
    assert other['state'] == 'COMPANY_REVIEW'
    with pytest.raises(DomainError) as error:
        confirm_context(w, reader, other, key='first-use-other-source')
    assert error.value.code == 'READER_NOT_READY'
    with w.db.connect() as c:
        assert not w.db.list(c, w.company, 'journal')
        assert c.execute('SELECT content FROM blobs WHERE company_id=? AND sha256=?', (w.company, document['sha256'])).fetchone()['content'] == BARE


def test_first_use_later_krw_still_rejects_fraction_before_questions(w):
    reader = Ingestion(w.svc)
    owner = OwnerView(w.svc, reader.uploads)
    named = owner.confirm(w.owner, w.company, CompanyFactsInput(
        expected_revision=0, name='시험 회사 A', business_number='2222222222', mode='company', confirmed=True, instruments=[]))
    _reader, document = read_document(w, FRACTION_BARE, 'first-use-fraction')
    owner.confirm(w.owner, w.company, CompanyFactsInput(
        expected_revision=named['revision'], name='시험 회사 A', business_number='2222222222', mode='company', confirmed=True,
        instruments=[{'kind': 'bank', 'identifier': '123456789012', 'currency': 'KRW'}]))
    work = Casework(w.svc)
    work.refresh(w.owner, w.company)
    assert work.list(w.owner, w.company)[0]['questions'] == []
    document = reader.uploads.document(w.owner, w.company, document['id'])
    with pytest.raises(DomainError) as error:
        confirm_context(w, reader, document, key='first-use-fraction-krw')
    assert error.value.code == 'KRW_FRACTION_REVIEW'
    event = work.list(w.owner, w.company)[0]
    assert event['questions'] == [] and event['values']['gross'] == '1.25' and event['values']['currency'] is None
    assert event['accounting_complete'] is False
    rows = reader.records(w.owner, w.company, document['id'])['rows']
    assert rows[0]['values']['gross'] == '1.25' and rows[0]['values']['currency'] is None
    with w.db.connect() as c:
        assert not w.db.list(c, w.company, 'document_source_context')
        assert not w.db.list(c, w.company, 'journal')
        assert c.execute('SELECT content FROM blobs WHERE company_id=? AND sha256=?', (w.company, document['sha256'])).fetchone()['content'] == FRACTION_BARE

def _visible(value):
    if isinstance(value, list):
        return [_visible(item) for item in value]
    if isinstance(value, dict):
        hidden = {'id', 'created_at', 'updated_at', 'confirmed_by', 'document_id', 'source_hash', 'filename', 'signature'}
        return {key: _visible(item) for key, item in value.items() if key not in hidden}
    return value


def test_owner_account_response_is_identical_whether_other_company_registered_it(tmp_path, pristine):
    backup, company, owner, actors, other_company, other_owner = pristine
    account = {'kind': 'bank', 'identifier': '123456789012', 'currency': 'KRW'}

    worlds = {}
    other_documents = {}
    for name, register_other in (('absent', False), ('present', True)):
        target = tmp_path / f'{name}.sqlite3'
        shutil.copyfile(backup, target)
        db = Database(target)
        svc = Service(db)
        world = World(db, svc, company, owner, actors['reviewer'], actors['preparer'], actors['viewer'], other_company, other_owner)
        view = OwnerView(svc, Ingestion(svc).uploads)
        if register_other:
            view.confirm(other_owner, other_company, CompanyFactsInput(
                expected_revision=0, name='시험 회사 B', business_number='3333333333', mode='company', confirmed=True,
                instruments=[account]))
        raw = BARE
        uploads = Uploads(svc)
        batch = uploads.begin(other_owner, other_company, UploadBatchInput(
            idempotency_key=f'other-{name}', files=[{'client_file_id': 'source1', 'filename': '통장.csv', 'size_bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}]))
        uploads.put_chunk(other_owner, other_company, batch['id'], 'source1', 0, raw, hashlib.sha256(raw).hexdigest())
        done = uploads.complete(other_owner, other_company, batch['id'], batch['revision'])
        other_documents[name] = done['files'][0]['document']['id']
        worlds[name] = target

    def observe(path, other_document):
        app = create_app(path)
        with TestClient(app) as client:
            login = client.post('/api/login', json={'username': 'test-owner', 'password': PASSWORD})
            assert login.status_code == 200
            client.headers['x-csrf-token'] = login.json()['csrf']
            base = '/api/companies/' + company
            saved = client.post(base + '/owner/onboarding', json={
                'expected_revision': 0, 'name': '시험 회사 A', 'business_number': '2222222222', 'mode': 'company', 'confirmed': True,
                'instruments': [account]})
            home = client.get(base + '/owner/home')
            denial_body = {
                'expected_revision': 1, 'expected_context_revision': 0, 'source_hash': 'a' * 64, 'generation': 1,
                'identifier': account['identifier'], 'currency': 'KRW', 'confirmed': True, 'idempotency_key': 'same-denial-key01'}
            missing = client.post(base + '/documents/missing-document-id/source-context', json=denial_body)
            foreign = client.post(base + '/documents/' + other_document + '/source-context', json=denial_body)
        db = Database(path)
        with db.connect() as c:
            stored = db.list(c, company, 'company_facts')
            journals = db.list(c, company, 'journal')
        return {
            'status': saved.status_code,
            'body': _visible(saved.json()),
            'home_status': home.status_code,
            'actions': _visible(home.json().get('owner_actions')),
            'accounting_complete': home.json().get('accounting_complete'),
            'pending': home.json().get('pending_instrument_candidates'),
            'stored': _visible(stored),
            'journals': journals,
            'missing_document': (missing.status_code, missing.json()),
            'foreign_document': (foreign.status_code, foreign.json()),
        }

    absent = observe(worlds['absent'], other_documents['absent'])
    present = observe(worlds['present'], other_documents['present'])
    assert absent == present
    assert absent['status'] == 200
    assert absent['body']['instruments'] == [account]
    assert absent['accounting_complete'] is False and absent['journals'] == []
    assert absent['missing_document'] == absent['foreign_document']
    assert absent['missing_document'][0] == 404 and absent['missing_document'][1]['code'] == 'NOT_FOUND'
