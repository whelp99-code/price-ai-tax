"""Re-reading source documents must never overwrite evidence or accounting."""
from __future__ import annotations
import pytest
from price.extractions import Extractions, ReprocessInput
from price.ingestion import Ingestion
from price.db import DomainError
from price.owner import OwnerView, CompanyFactsInput
from test_source_ingestion import upload, BANK


def ready(w):
    batch=upload(w); reader=Ingestion(w.svc);reader.run_once(w.company)
    return reader,reader.uploads.document(w.owner,w.company,batch['files'][0]['document']['id'])


def request(doc,key='reprocess-case-01',reason='새 파서로 원본 다시 읽기'):
    return ReprocessInput(expected_revision=doc['revision'],idempotency_key=key,reason=reason)


def test_reprocess_retains_prior_generation_and_rebuilds_without_collision(w):
    reader,doc=ready(w);service=Extractions(w.svc)
    original=w.svc.evidence_content(w.owner,w.company,doc['blob_id'])['content']
    old=reader.records(w.owner,w.company,doc['id'])['rows']
    queued=service.reprocess(w.owner,w.company,doc['id'],request(doc))
    assert queued['generation']==2 and queued['state']=='QUEUED'
    assert reader.records(w.owner,w.company,doc['id'])['total']==0
    result=reader.run_once(w.company)
    assert result['state']=='SUCCEEDED' and result['records']==2,result
    current=reader.records(w.owner,w.company,doc['id'])['rows']
    assert {r['id'] for r in old}.isdisjoint({r['id'] for r in current})
    assert [r['values'] for r in current]==[r['values'] for r in old]
    assert w.svc.evidence_content(w.owner,w.company,doc['blob_id'])['content']==original
    history=service.history(w.owner,w.company,doc['id'])
    assert [r['generation'] for r in history['runs']]==[1,2]
    assert history['runs'][-1]['value_changes']==[]
    with w.db.connect() as c:
        assert len(w.db.list(c,w.company,'source_record'))==4
        cases=w.db.list(c,w.company,'ingestion_case')
        assert sum(x['state']=='SUPERSEDED' for x in cases)==2
        assert sum(x['state']=='WAITING_FOR_LINKING' for x in cases)==2
        assert not w.db.list(c,w.company,'journal')
        assert w.db.verify_audit(c,w.company)['valid']


def test_reprocess_key_is_idempotent_and_payload_bound(w):
    reader,doc=ready(w);service=Extractions(w.svc);data=request(doc)
    one=service.reprocess(w.owner,w.company,doc['id'],data)
    two=service.reprocess(w.owner,w.company,doc['id'],data)
    assert two['generation']==one['generation'] and two['reused']
    with pytest.raises(DomainError) as error:
        service.reprocess(w.owner,w.company,doc['id'],request(doc,reason='다른 실행 요청'))
    assert error.value.code=='REPROCESS_KEY_CONFLICT'


def test_stale_revision_cannot_retire_current_sources(w):
    reader,doc=ready(w);service=Extractions(w.svc)
    with pytest.raises(DomainError) as error:
        service.reprocess(w.owner,w.company,doc['id'],request({**doc,'revision':1}))
    assert error.value.code=='STALE_VERSION'
    assert reader.records(w.owner,w.company,doc['id'])['total']==2


@pytest.mark.parametrize('role',['viewer','other_owner'])
def test_reprocess_is_company_writer_scoped(w,role):
    reader,doc=ready(w);service=Extractions(w.svc)
    with pytest.raises(DomainError):service.reprocess(getattr(w,role),w.company,doc['id'],request(doc))
    assert reader.records(w.owner,w.company,doc['id'])['total']==2


def test_inflight_reader_cannot_be_replaced(w):
    reader,doc=ready(w);service=Extractions(w.svc)
    service.reprocess(w.owner,w.company,doc['id'],request(doc))
    job=reader.claim(w.company)
    current=reader.uploads.document(w.owner,w.company,doc['id'])
    with pytest.raises(DomainError) as error:
        service.reprocess(w.owner,w.company,doc['id'],request(current,key='reprocess-case-02'))
    assert error.value.code=='REPROCESS_BUSY'
    from price.source_tables import parse_source_table
    assert reader.finish(job,parse_source_table(BANK))['committed']


def test_corrected_company_facts_can_recover_held_source(w):
    reader,doc=ready(w);owner=OwnerView(w.svc,reader.uploads)
    first=owner.confirm(w.owner,w.company,CompanyFactsInput(expected_revision=0,name='시험 회사 A',mode='company',confirmed=True,instruments=[{'kind':'bank','identifier':'999999999999'}]))
    owner.confirm(w.owner,w.company,CompanyFactsInput(expected_revision=first['revision'],name='시험 회사 A',mode='company',confirmed=True,instruments=[{'kind':'bank','identifier':'123456789012'}]))
    held=reader.uploads.document(w.owner,w.company,doc['id'])
    Extractions(w.svc).reprocess(w.owner,w.company,doc['id'],request(held))
    assert reader.run_once(w.company)['state']=='SUCCEEDED'
    assert reader.uploads.document(w.owner,w.company,doc['id'])['state']=='NORMALIZED'


def test_failed_retry_does_not_expose_old_results_as_current(w,monkeypatch):
    reader,doc=ready(w);service=Extractions(w.svc);service.reprocess(w.owner,w.company,doc['id'],request(doc))
    monkeypatch.setattr('price.ingestion.parse_text_document',lambda raw: {'state':'READ_FAILED','records':[],'issues':[{'code':'READER_FAILED'}]})
    assert reader.run_once(w.company)['state']=='BLOCKED'
    assert reader.records(w.owner,w.company,doc['id'])['total']==0
    assert len(service.history(w.owner,w.company,doc['id'])['runs'])==2


def test_changed_parser_outputs_create_field_diff_and_impact(w,monkeypatch):
    from price.source_tables import parse_source_table
    reader,doc=ready(w);service=Extractions(w.svc)
    service.reprocess(w.owner,w.company,doc['id'],request(doc))
    result=parse_source_table(BANK)
    result['records'][0]['values']['description']='파서가 다시 읽은 다른 설명'
    result['parser_version']='synthetic-test-reader/2'
    monkeypatch.setattr('price.ingestion.parse_text_document',lambda raw: result)
    assert reader.run_once(w.company)['state']=='SUCCEEDED'
    latest=service.history(w.owner,w.company,doc['id'])['runs'][-1]
    assert any(x['field']=='description' for x in latest['value_changes'])
    with w.db.connect() as c:
        impacts=w.db.list(c,w.company,'source_change_impact')
        assert impacts and impacts[-1]['state']=='REVIEW_REQUIRED'


def test_posted_original_cannot_be_changed_by_reprocessing(w):
    reader,doc=ready(w)
    posted=w.post(w.tx(evidence_ids=[doc['blob_id']]))
    service=Extractions(w.svc);service.reprocess(w.owner,w.company,doc['id'],request(doc))
    reader.run_once(w.company)
    with w.db.connect() as c:
        assert w.db.get(c,w.company,'journal',posted['id'])==posted
        assert any(posted['id'] in x['posted_journal_ids'] for x in w.db.list(c,w.company,'source_change_impact'))


def test_history_and_coverage_require_same_company(w):
    reader,doc=ready(w);service=Extractions(w.svc)
    with pytest.raises(DomainError): service.history(w.other_owner,w.company,doc['id'])
    with pytest.raises(DomainError): service.coverage(w.other_owner,w.company,doc['id'])
    coverage=service.coverage(w.owner,w.company,doc['id'])
    assert coverage['record_count']==2 and coverage['source_rows']==2
    assert coverage['company_period_complete'] is False and coverage['accounting_complete'] is False


def test_quarantined_file_never_reprocessed_as_safe(w):
    batch=upload(w,b'not a real PDF',filename='손상.pdf')
    doc=Ingestion(w.svc).uploads.document(w.owner,w.company,batch['files'][0]['document']['id'])
    with pytest.raises(DomainError) as error:
        Extractions(w.svc).reprocess(w.owner,w.company,doc['id'],request(doc))
    assert error.value.code=='DOCUMENT_NOT_REPROCESSABLE'
