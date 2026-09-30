from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import pytest
from pydantic import ValidationError
from price.casework import Casework, FactAnswer
from price.owner import OwnerView, CompanyFactsInput
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
