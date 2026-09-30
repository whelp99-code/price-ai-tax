from __future__ import annotations
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from price.autopolicies import PolicyEngine,PolicyInput,PolicyReview,PolicyTruthCase,PolicyAction
from price.models import AuthorityInput
from price.db import DomainError
from price.auth import Actor
from test_casework import setup_sources,invoice,answer

EXPECTED=[{'account':'expense','side':'debit','amount':1000},{'account':'vat_input','side':'debit','amount':100},{'account':'payable','side':'credit','amount':1100}]


def prepared(w,*,enabled=False):
    work=setup_sources(w);event=invoice(work,w)
    answer(work,w,event,'business_use','business');answer(work,w,event,'use_type','operations',key='policy-purpose-01')
    authority=w.svc.add_authority(w.owner,w.company,AuthorityInput(title='SYNTHETIC policy test, not tax advice',url='https://www.law.go.kr',effective_from='2020-01-01',effective_to='2099-12-31',excerpt='Synthetic fixture; not an asserted legal source or professional signoff.',kind='sample'))
    authority=w.svc.review_authority(w.reviewer,w.company,authority['id'],authority['revision'],'synthetic fixture review')
    engine=PolicyEngine(w.svc,posting_enabled=enabled)
    policy=engine.create(w.owner,w.company,PolicyInput(name='Synthetic narrow supplier purchase',direction='purchase',counterparties=['1111111111'],use_type='operations',tax_category='standard',deductible=True,amount_limit=10000,effective_from='2026-01-01',effective_to='2026-12-31',authority_ids=[authority['id']]))
    return engine,policy,event,work


def activated(w,*,enabled=False):
    engine,policy,event,work=prepared(w,enabled=enabled)
    policy=engine.review(w.reviewer,w.company,policy['id'],PolicyReview(expected_revision=policy['revision'],cases=[PolicyTruthCase(event_id=event['id'],expected_lines=EXPECTED)],reason='independent fixture comparison'))
    policy=engine.activate(w.owner,w.company,policy['id'],PolicyAction(expected_revision=policy['revision'],reason='synthetic validation only'))
    return engine,policy,event,work


def test_unreviewed_policy_cannot_activate(w):
    engine,policy,event,_=prepared(w)
    with pytest.raises(DomainError):engine.activate(w.owner,w.company,policy['id'],PolicyAction(expected_revision=policy['revision'],reason='must not bypass'))


def test_wrong_independent_truth_rejects_policy(w):
    engine,policy,event,_=prepared(w)
    wrong=[{**x,'amount':x['amount']+1} for x in EXPECTED]
    with pytest.raises(DomainError) as err:engine.review(w.reviewer,w.company,policy['id'],PolicyReview(expected_revision=policy['revision'],cases=[PolicyTruthCase(event_id=event['id'],expected_lines=wrong)],reason='must fail'))
    assert err.value.code=='POLICY_TRUTH_MISMATCH'


def test_default_runtime_never_enables_posting(w):
    engine,policy,event,_=activated(w)
    auth=engine.evaluate(w.owner,w.company,policy['id'],event['id'])
    with pytest.raises(DomainError) as err:engine.execute(w.owner,w.company,auth['id'])
    assert err.value.code=='POLICY_EXECUTION_NOT_RELEASED'
    with w.db.connect() as c:assert not w.db.list(c,w.company,'journal')


def test_isolated_enabled_executor_uses_authorization_once(w):
    engine,policy,event,_=activated(w,enabled=True)
    auth=engine.evaluate(w.owner,w.company,policy['id'],event['id'])
    result=engine.execute(w.owner,w.company,auth['id'])
    repeated=engine.execute(w.owner,w.company,auth['id'])
    assert result['id']==repeated['id'] and result['state']=='POSTED'
    assert result['approved_by'] is None and result['authorization_kind']=='APPROVED_POLICY'
    assert result['lines']==EXPECTED
    with w.db.connect() as c:
        assert len(w.db.list(c,w.company,'journal'))==1
        assert w.db.verify_audit(c,w.company)['valid']
        with pytest.raises(Exception):c.execute("DELETE FROM objects WHERE company_id=? AND kind='journal'",(w.company,))


def test_fact_change_invalidates_unused_authorization(w):
    engine,policy,event,work=activated(w,enabled=True);auth=engine.evaluate(w.owner,w.company,policy['id'],event['id'])
    answer(work,w,event,'business_use','personal',key='change-policy-facts',expected_fact_revision=1)
    with pytest.raises(DomainError):engine.execute(w.owner,w.company,auth['id'])
    with w.db.connect() as c:assert not w.db.list(c,w.company,'journal')


def test_policy_revocation_blocks_pending_authorization(w):
    engine,policy,event,_=activated(w,enabled=True);auth=engine.evaluate(w.owner,w.company,policy['id'],event['id'])
    result=engine.revoke(w.reviewer,w.company,policy['id'],PolicyAction(expected_revision=policy['revision'],reason='suspend policy'))
    assert not result['posted_records_changed']
    with pytest.raises(DomainError):engine.execute(w.owner,w.company,auth['id'])


def test_revoked_reviewer_role_invalidates_policy_execution(w):
    engine,policy,event,_=activated(w,enabled=True);auth=engine.evaluate(w.owner,w.company,policy['id'],event['id'])
    with w.db.transaction() as c:c.execute('DELETE FROM memberships WHERE user_id=? AND company_id=?',(w.reviewer.id,w.company))
    with pytest.raises(DomainError):engine.execute(w.owner,w.company,auth['id'])


def test_worker_cannot_become_policy_executor(w):
    engine,policy,event,_=activated(w,enabled=True);auth=engine.evaluate(w.owner,w.company,policy['id'],event['id'])
    with pytest.raises(DomainError) as err:engine.execute(Actor('worker','worker',worker=True),w.company,auth['id'])
    assert err.value.code=='WORKER_FORBIDDEN'


def test_concurrent_execution_conserves_single_ledger_effect(w):
    engine,policy,event,_=activated(w,enabled=True);auth=engine.evaluate(w.owner,w.company,policy['id'],event['id'])
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:engine.execute(w.owner,w.company,auth['id']),[1,2]))
    assert results[0]['id']==results[1]['id']


def test_posted_policy_revocation_produces_impact_not_rollback(w):
    engine,policy,event,_=activated(w,enabled=True);auth=engine.evaluate(w.owner,w.company,policy['id'],event['id']);j=engine.execute(w.owner,w.company,auth['id'])
    impact=engine.revoke(w.reviewer,w.company,policy['id'],PolicyAction(expected_revision=policy['revision'],reason='recheck prior effects'))
    assert impact['posted_review_ids']==[j['id']]
    assert w.svc.get_object(w.owner,w.company,'journal',j['id'])==j


def test_duplicate_truth_case_does_not_inflate_validation(w):
    engine,policy,event,_=prepared(w);case=PolicyTruthCase(event_id=event['id'],expected_lines=EXPECTED)
    with pytest.raises(DomainError) as err:engine.review(w.reviewer,w.company,policy['id'],PolicyReview(expected_revision=policy['revision'],cases=[case,case],reason='duplicate is not separate validation'))
    assert err.value.code=='POLICY_TRUTH_DUPLICATE'


def test_other_company_cannot_use_posting_authorization(w):
    engine,policy,event,_=activated(w,enabled=True);auth=engine.evaluate(w.owner,w.company,policy['id'],event['id'])
    with pytest.raises(DomainError):engine.execute(w.other_owner,w.company,auth['id'])
