import pytest
from price.owner import CompanyFactsInput,OwnerView
from price.uploads import Uploads
from price.ingestion import Ingestion
from test_source_ingestion import upload,BANK,INVOICES
from test_native_documents import REGISTRATION


def test_later_ownership_confirmation_invalidates_prior_ready_sources(w):
    batch=upload(w)
    reader=Ingestion(w.svc);assert reader.run_once(w.company)['cases']==2
    owner=OwnerView(w.svc,reader.uploads)
    owner.confirm(w.owner,w.company,CompanyFactsInput(expected_revision=0,name='시험 회사 A',mode='company',confirmed=True,instruments=[{'kind':'bank','identifier':'999999999999'}]))
    document=reader.uploads.document(w.owner,w.company,batch['files'][0]['document']['id'])
    assert document['state']=='COMPANY_REVIEW'
    with w.db.connect() as c:
        cases=w.db.list(c,w.company,'ingestion_case')
        assert len(cases)==2 and all(x['state']=='CONTEXT_REVIEW' for x in cases)
        assert len(w.db.list(c,w.company,'source_record'))==2
        assert not w.db.list(c,w.company,'journal')
        assert w.db.verify_audit(c,w.company)['valid']


def test_confirmed_masked_identifier_does_not_falsely_prove_mismatch(w):
    batch=upload(w);reader=Ingestion(w.svc);reader.run_once(w.company)
    OwnerView(w.svc,reader.uploads).confirm(w.owner,w.company,CompanyFactsInput(expected_revision=0,name='시험 회사 A',mode='company',confirmed=True,instruments=[{'kind':'bank','identifier':'123***789012'}]))
    assert reader.uploads.document(w.owner,w.company,batch['files'][0]['document']['id'])['state']=='NORMALIZED'


def test_conflicting_company_registration_is_held_after_identity_confirmed(w):
    reader=Ingestion(w.svc)
    OwnerView(w.svc,reader.uploads).confirm(w.owner,w.company,CompanyFactsInput(expected_revision=0,name='시험 회사 A',mode='company',confirmed=True,business_number='9999999999'))
    upload(w,REGISTRATION.encode(),filename='타회사등록증.txt')
    result=reader.run_once(w.company)
    assert result['state']=='BLOCKED' and result['error']=='COMPANY_SOURCE_CONFLICT'


def test_later_invoice_identity_conflict_keeps_original(w):
    batch=upload(w,INVOICES);reader=Ingestion(w.svc);reader.run_once(w.company)
    OwnerView(w.svc,reader.uploads).confirm(w.owner,w.company,CompanyFactsInput(expected_revision=0,name='시험 회사 A',mode='company',confirmed=True,business_number='9999999999'))
    document=reader.uploads.document(w.owner,w.company,batch['files'][0]['document']['id'])
    assert document['state']=='COMPANY_REVIEW'
    assert w.svc.evidence_content(w.owner,w.company,document['blob_id'])['content']==INVOICES
