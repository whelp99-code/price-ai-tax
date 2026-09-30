from __future__ import annotations
import hashlib
import pytest
from price.submission_gateway import SubmissionGateway,PackageInput,PackageRevision
from price.db import DomainError


class FakeAdapter:
    name='synthetic';environment='sandbox'
    def __init__(self,mode='received'):self.mode=mode;self.calls=0;self.saved={}
    def submit(self,*,operation_id,document,metadata):
        self.calls+=1
        result={key:metadata[key] for key in ('document_hash','taxpayer_reference','tax_type','period')}
        result.update(external_id='SIM-'+operation_id,status='received')
        self.saved[operation_id]=result
        if self.mode=='timeout':raise TimeoutError('simulate lost response; no real channel')
        if self.mode=='wrong-party':result['taxpayer_reference']='9999999999'
        if self.mode=='reject':result['status']='rejected'
        return result
    def reconcile(self,*,operation_id,metadata):return self.saved[operation_id]


def prepared(w,mode='received',configured=True):
    artifact=w.svc.evidence_content(w.owner,w.company,w.evidence)
    with w.db.transaction() as c:
        proof=w.db.create(c,w.company,'form_validation',{'state':'VALIDATED','document_hash':artifact['sha256'],'environment':'sandbox','tax_type':'VAT','period':'2026-Q3','taxpayer_reference':'1111111111','institution_validated':False,'fixture_only':True})
    fake=FakeAdapter(mode);gateway=SubmissionGateway(w.svc,{'synthetic':fake} if configured else {})
    p=gateway.prepare(w.owner,w.company,PackageInput(artifact_id=w.evidence,taxpayer_reference='1111111111',tax_type='VAT',period='2026-Q3',adapter_name='synthetic',environment='sandbox',form_validation_reference=proof['id'],idempotency_key='gateway-package-01'))
    return gateway,p,fake


def authorized(w,mode='received',configured=True):
    g,p,f=prepared(w,mode,configured)
    p=g.review(w.reviewer,w.company,p['id'],PackageRevision(expected_revision=p['revision'],reason='synthetic independent check'))
    p=g.authorize(w.owner,w.company,p['id'],PackageRevision(expected_revision=p['revision'],reason='synthetic submission only'))
    return g,p,f


def test_missing_channel_does_not_create_attempt_or_fake_receipt(w):
    g,p,f=authorized(w,configured=False)
    with pytest.raises(DomainError) as error:g.submit(w.owner,w.company,p['id'])
    assert error.value.code=='CHANNEL_NOT_CONFIGURED' and f.calls==0
    with w.db.connect() as c:assert not w.db.list(c,w.company,'gateway_attempt')


def test_successful_sandbox_receipt_remains_simulated_and_unpaid(w):
    g,p,f=authorized(w)
    result=g.submit(w.owner,w.company,p['id'])
    assert result['state']=='SIM_RECEIVED' and result['paid'] is False
    with pytest.raises(DomainError):g.submit(w.owner,w.company,p['id'])
    assert f.calls==1


def test_timeout_never_causes_blind_resubmission(w):
    g,p,f=authorized(w,'timeout')
    result=g.submit(w.owner,w.company,p['id'])
    assert result['state']=='OUTCOME_UNKNOWN'
    with pytest.raises(DomainError):g.submit(w.owner,w.company,p['id'])
    assert f.calls==1
    reconciled=g.reconcile(w.owner,w.company,p['id'])
    assert reconciled['state']=='SIM_RECEIVED' and not reconciled['paid']
    assert f.calls==1


def test_receipt_for_wrong_taxpayer_not_accepted(w):
    g,p,f=authorized(w,'wrong-party')
    result=g.submit(w.owner,w.company,p['id'])
    assert result['state']=='OUTCOME_UNKNOWN' and result['receipt'] is None


def test_rejected_receipt_not_success_or_paid(w):
    g,p,f=authorized(w,'reject');result=g.submit(w.owner,w.company,p['id'])
    assert result['state']=='REJECTED' and result['paid'] is False


def test_self_review_and_unauthorized_execution_blocked(w):
    g,p,f=prepared(w)
    with pytest.raises(DomainError):g.review(w.owner,w.company,p['id'],PackageRevision(expected_revision=p['revision'],reason='self must fail'))
    with pytest.raises(DomainError):g.submit(w.owner,w.company,p['id'])
    assert f.calls==0


def test_company_and_role_guard_on_submission(w):
    g,p,f=authorized(w)
    for actor in (w.other_owner,w.reviewer,w.viewer):
        with pytest.raises(DomainError):g.submit(actor,w.company,p['id'])
    assert f.calls==0


def test_simulator_cannot_be_used_as_live_adapter(w):
    g,p,f=authorized(w)
    with w.db.transaction() as c:
        current=w.db.get(c,w.company,'submission_package',p['id'])
        w.db.replace(c,w.company,'submission_package',p['id'],{**current,'environment':'live'},current['revision'])
    with pytest.raises(DomainError):g.submit(w.owner,w.company,p['id'])
    assert f.calls==0


def test_changed_package_metadata_breaks_snapshot(w):
    g,p,f=authorized(w)
    with w.db.transaction() as c:
        current=w.db.get(c,w.company,'submission_package',p['id'])
        w.db.replace(c,w.company,'submission_package',p['id'],{**current,'period':'2026-Q4'},current['revision'])
    with pytest.raises(DomainError) as error:g.submit(w.owner,w.company,p['id'])
    assert error.value.code=='PACKAGE_SNAPSHOT_CHANGED' and f.calls==0


def test_form_validator_must_match_artifact_taxpayer_period(w):
    g,p,f=prepared(w)
    data=PackageInput(artifact_id=w.evidence,taxpayer_reference='1111111111',tax_type='CORPORATE',period='2026-Q3',adapter_name='synthetic',form_validation_reference=p['form_validation_reference'],idempotency_key='different-form-01')
    with pytest.raises(DomainError) as error:g.prepare(w.owner,w.company,data)
    assert error.value.code=='FORM_VALIDATION_REQUIRED'
