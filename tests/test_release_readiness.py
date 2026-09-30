from datetime import datetime,timedelta,timezone
import hashlib,hmac,json
import pytest
from price.release_readiness import evaluate_evidence,verified_envelope,VERSION_REQUIREMENTS,REAL_REQUIRED,LIVE_REQUIRED,ReleaseReadiness
from price.db import canonical,DomainError


def synthetic_test_evidence():
    return [{'version':v,'requirement':r,'state':'VERIFIED','source_revision_current':True,'artifact_sha256':'a'*64,'author':'fixture-author','reviewer':'fixture-reviewer','failures':0,'kind':'SYNTHETIC_TEST'} for v,requirements in VERSION_REQUIREMENTS.items() for r in requirements if r!='all_prior_versions']


def synthetic_complete_gate_fixture():
    # This is test data for the evaluator, never installed into release evidence.
    records=synthetic_test_evidence()
    for record in records:
        r=record['requirement']
        if r in REAL_REQUIRED:record['kind']='REAL_FORMAT'
        if r in LIVE_REQUIRED:record['kind']='LIVE'
        if r=='thirty_day_observation':record.update(kind='USER_OBSERVATION',observed_days=30)
        if r=='novice_acceptance':record.update(kind='USER_OBSERVATION',participants=5,account_switches=0,accounting_inputs=0)
    return records


def test_code_and_synthetic_tests_never_pass_real_release_gates():
    result=evaluate_evidence(synthetic_test_evidence())
    assert not result['final_accepted'] and not any(v['accepted'] for v in result['versions'])
    assert len(result['versions'])==10


def test_gate_evaluator_accepts_complete_independent_evidence_structure():
    result=evaluate_evidence(synthetic_complete_gate_fixture())
    assert result['final_accepted']
    assert all(v['accepted'] for v in result['versions'])


def test_missing_live_gate_cannot_be_carried_into_final_version():
    records=[r for r in synthetic_complete_gate_fixture() if r['requirement']!='live_tax_receipts']
    result=evaluate_evidence(records)
    assert result['versions'][7]['accepted']
    assert not result['versions'][8]['accepted'] and not result['versions'][9]['accepted']


@pytest.mark.parametrize('field,value',[('reviewer','fixture-author'),('artifact_sha256','not-a-hash'),('source_revision_current',False),('failures',True),('failures',1)])
def test_invalid_attestation_does_not_count(field,value):
    records=synthetic_complete_gate_fixture();records[0][field]=value
    assert not evaluate_evidence(records)['final_accepted']


def test_observation_period_and_novice_experience_not_replaced_by_unit_tests():
    records=synthetic_complete_gate_fixture()
    next(x for x in records if x['requirement']=='thirty_day_observation')['observed_days']=1
    assert not evaluate_evidence(records)['final_accepted']
    records=synthetic_complete_gate_fixture();next(x for x in records if x['requirement']=='novice_acceptance')['account_switches']=1
    assert not evaluate_evidence(records)['final_accepted']


def envelope(records,key=b'synthetic-test-key-not-secret-0001',**changes):
    payload={'schema':'price.signed-acceptance/1','component_hash':'b'*64,'expires_at':(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),'records':records}
    payload.update(changes)
    signature=hmac.new(key,canonical(payload).encode(),hashlib.sha256).hexdigest()
    return json.dumps({**payload,'signature':signature}).encode(),key


def test_signed_evidence_binds_code_and_expiry():
    raw,key=envelope(synthetic_test_evidence())
    assert verified_envelope(raw,key,'b'*64)
    with pytest.raises(ValueError):verified_envelope(raw,key,'c'*64)
    with pytest.raises(ValueError):verified_envelope(raw,b'different-synthetic-key-0000000000','b'*64)
    raw,key=envelope([],expires_at=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
    with pytest.raises(ValueError):verified_envelope(raw,key,'b'*64)


def test_unsigned_or_modified_record_is_rejected():
    raw,key=envelope([]);value=json.loads(raw);value['records']=synthetic_complete_gate_fixture()
    with pytest.raises(ValueError):verified_envelope(json.dumps(value).encode(),key,'b'*64)


def test_runtime_report_starts_unaccepted_without_provisioned_evidence(w):
    report=ReleaseReadiness(w.svc).report(w.owner,w.company)
    assert not report['final_accepted'] and report['evidence_records_loaded']==0
    assert report['evidence_signature_status']=='NOT_PROVISIONED'
    with pytest.raises(DomainError):ReleaseReadiness(w.svc).report(w.other_owner,w.company)
