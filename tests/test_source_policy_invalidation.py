"""Cross-stage regressions: newly read documents cannot revive stale posting authority."""
from __future__ import annotations
import pytest
from price.db import DomainError
from price.extractions import Extractions, ReprocessInput
from price.ingestion import Ingestion
from price.casework import Casework
from price.owner import OwnerView, CompanyFactsInput
from test_autopolicies import activated
from test_source_ingestion import upload
from test_source_xml import invoice_xml


def test_reprocessing_immediately_invalidates_existing_policy_authority(w):
    engine,policy,event,work=activated(w,enabled=True)
    authorization=engine.evaluate(w.owner,w.company,policy['id'],event['id'])
    extractions=Extractions(w.svc)
    doc=extractions.uploads.document(w.owner,w.company,event['document_ids'][0])
    extractions.reprocess(w.owner,w.company,doc['id'],ReprocessInput(expected_revision=doc['revision'],idempotency_key='policy-reprocess-0001',reason='새 추출 버전 검증'))
    with pytest.raises(DomainError):engine.execute(w.owner,w.company,authorization['id'])
    assert Ingestion(w.svc).run_once(w.company)['state']=='SUCCEEDED'
    work.refresh(w.owner,w.company)
    with pytest.raises(DomainError):engine.execute(w.owner,w.company,authorization['id'])
    with w.db.connect() as c:
        assert not w.db.list(c,w.company,'journal')
        assert w.db.verify_audit(c,w.company)['valid']


def test_replay_after_posting_creates_impact_preserves_exact_posted_record(w):
    engine,policy,event,work=activated(w,enabled=True)
    authorization=engine.evaluate(w.owner,w.company,policy['id'],event['id'])
    posted=engine.execute(w.owner,w.company,authorization['id'])
    extractions=Extractions(w.svc)
    doc=extractions.uploads.document(w.owner,w.company,event['document_ids'][0])
    extractions.reprocess(w.owner,w.company,doc['id'],ReprocessInput(expected_revision=doc['revision'],idempotency_key='posted-source-replay-1',reason='원문 재확인'))
    Ingestion(w.svc).run_once(w.company);work.refresh(w.owner,w.company)
    with w.db.connect() as c:
        assert w.db.get(c,w.company,'journal',posted['id'])==posted
        assert any(posted['id'] in x['posted_journal_ids'] for x in w.db.list(c,w.company,'source_change_impact'))


def test_positive_xml_amendment_without_prior_reference_is_not_normal_purchase(w):
    reader=Ingestion(w.svc)
    OwnerView(w.svc,reader.uploads).confirm(w.owner,w.company,CompanyFactsInput(expected_revision=0,name='시험 회사 A',business_number='2222222222',mode='company',confirmed=True))
    raw=invoice_xml(extra='<AmendmentStatusCode>01</AmendmentStatusCode>',currency=' currencyID="KRW"')
    upload(w,raw,filename='수정세금계산서.xml')
    assert reader.run_once(w.company)['state']=='SUCCEEDED'
    work=Casework(w.svc);work.refresh(w.owner,w.company)
    event=work.list(w.owner,w.company)[0]
    assert 'CORRECTION_REVIEW' in event['issues']
    assert event['state']=='REVIEW_CONNECTION_NEEDED'
    assert event['questions']==[] and not event['accounting_complete']


def test_unverified_xml_signature_is_not_erased_by_economic_event(w):
    reader=Ingestion(w.svc)
    OwnerView(w.svc,reader.uploads).confirm(w.owner,w.company,CompanyFactsInput(expected_revision=0,name='시험 회사 A',business_number='2222222222',mode='company',confirmed=True))
    upload(w,invoice_xml(currency=' currencyID="KRW"'),filename='세금계산서.xml')
    reader.run_once(w.company)
    work=Casework(w.svc);work.refresh(w.owner,w.company)
    event=work.list(w.owner,w.company)[0]
    assert event.get('source_signature_verified') is False
    assert event.get('source_statutory_validated') is False
