"""Real SQLite posting through owner consent; no fake reviewer or external API."""
from datetime import datetime,timezone,timedelta
from concurrent.futures import ThreadPoolExecutor
import pytest
from price.automation import AutomaticBooks,AutomationConsent,AutomationAction
from price.autopolicies import PolicyInput,PolicyReview,PolicyTruthCase,PolicyAction
from price.db import DomainError
from price.ingestion import Ingestion
from test_autopolicies import activated,EXPECTED
from test_casework import invoice,answer
from test_source_ingestion import upload,INVOICES


def consent(engine,w,policy,**changes):
    values={'policy_ids':[policy['id']],'period_start':'2026-01-01','period_end':'2026-12-31',
            'expires_at':datetime.now(timezone.utc)+timedelta(days=3),'max_per_event':10000,'max_total':20000,
            'max_events':20,'idempotency_key':'consent-synthetic-01','confirmed':True}
    values.update(changes)
    return engine.consent(w.owner,w.company,AutomationConsent(**values))


def ready(w,enabled=True,**changes):
    policies,policy,event,work=activated(w)
    service=AutomaticBooks(w.svc,execution_enabled=enabled)
    grant=consent(service,w,policy,**changes)
    return service,grant,event,policies,work


def test_source_facts_then_single_worker_iteration_posts_without_owner_journal_input(w):
    engine,g,event,_,_=ready(w)
    out=engine.run_once(w.company)
    assert out['processed'] and out['state']=='POSTED' and out['journal_count']==1
    assert not engine.run_once(w.company)['processed']
    state=engine.status(w.owner,w.company)
    assert state['posted_events']==1 and state['posted_gross']==1100
    assert state['grants'][0]['processed_events']==1
    with w.db.connect() as c:
        journals=w.db.list(c,w.company,'journal')
        assert len(journals)==1 and journals[0]['lines']==EXPECTED
        assert journals[0]['authorization_kind']=='APPROVED_POLICY' and journals[0]['approved_by'] is None
        assert w.db.verify_audit(c,w.company)['valid']


def test_default_deployment_does_not_bypass_policy_release_switch(w):
    engine,g,event,_,_=ready(w,enabled=False)
    assert engine.run_once()['state']=='EXECUTION_DISABLED'
    with pytest.raises(DomainError) as e:engine.process(w.company,g['id'],event['id'])
    assert e.value.code=='AUTOMATION_EXECUTION_DISABLED'
    with w.db.transaction() as c:
        with pytest.raises(DomainError):engine.policies._execute_in(c,w.owner,w.company,'anything')
        assert not w.db.list(c,w.company,'journal')


def test_concurrent_execution_consumes_budget_once(w):
    engine,g,event,_,_=ready(w,max_total=1100,max_per_event=1100,max_events=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:engine.process(w.company,g['id'],event['id']),range(2)))
    assert len({r['journal_id'] for r in results})==1
    assert sum(not r['reused'] for r in results)==1
    with w.db.connect() as c:
        grant=w.db.get(c,w.company,'automation_grant',g['id'])
        assert grant['processed_events']==1 and grant['processed_gross']==1100


def test_ledger_and_cap_usage_rollback_together_on_failure(w,monkeypatch):
    engine,g,event,_,_=ready(w)
    original=engine.policies._execute_in
    def fail(c,actor,company,ident):
        original(c,actor,company,ident)
        raise DomainError('SYNTHETIC_FAILURE','rollback test')
    monkeypatch.setattr(engine.policies,'_execute_in',fail)
    with pytest.raises(DomainError):engine.process(w.company,g['id'],event['id'])
    with w.db.connect() as c:
        assert not w.db.list(c,w.company,'journal')
        assert not w.db.list(c,w.company,'transaction')
        assert not w.db.list(c,w.company,'posting_authorization')
        assert w.db.get(c,w.company,'automation_grant',g['id'])['processed_events']==0
        assert w.db.verify_audit(c,w.company)['valid']


def test_revoked_consent_or_reviewer_cannot_execute_pending_event(w):
    engine,g,event,policies,_=ready(w)
    engine.stop(w.owner,w.company,g['id'],AutomationAction(expected_revision=g['revision'],reason='대표의 자동 처리 중지'))
    with pytest.raises(DomainError):engine.process(w.company,g['id'],event['id'])
    with w.db.connect() as c:assert not w.db.list(c,w.company,'journal')


def test_role_revoked_after_consent_is_checked_on_actual_post(w):
    engine,g,event,_,_=ready(w)
    with w.db.transaction() as c:c.execute('DELETE FROM memberships WHERE user_id=? AND company_id=?',(w.reviewer.id,w.company))
    with pytest.raises(DomainError):engine.process(w.company,g['id'],event['id'])
    assert engine.status(w.owner,w.company)['held']


def test_tampered_limits_and_expired_grant_cannot_run(w):
    engine,g,event,_,_=ready(w)
    engine.clock=lambda:datetime.now(timezone.utc).timestamp()+5*86400
    with pytest.raises(DomainError) as e:engine.process(w.company,g['id'],event['id'])
    assert e.value.code=='AUTOMATION_EXPIRED'
    engine.clock=lambda:datetime.now(timezone.utc).timestamp()
    with w.db.transaction() as c:
        old=w.db.get(c,w.company,'automation_grant',g['id']);old['spec']['max_total']+=1000
        w.db.replace(c,w.company,'automation_grant',g['id'],old,old['revision'])
    with pytest.raises(DomainError) as e:engine.process(w.company,g['id'],event['id'])
    assert e.value.code=='AUTOMATION_STOPPED'


def test_owner_fact_change_stops_auto_post_without_asking_tax_questions(w):
    engine,g,event,_,work=ready(w)
    answer(work,w,event,'business_use','personal',key='change-auto-fact',expected_fact_revision=1)
    with pytest.raises(DomainError):engine.process(w.company,g['id'],event['id'])
    status=engine.status(w.owner,w.company)
    assert any(h['code']=='AUTOMATION_NO_POLICY' for h in status['held'])


def test_two_matching_policies_are_not_silently_chosen(w):
    policies,p,event,work=activated(w)
    p2=policies.create(w.owner,w.company,PolicyInput(**{**p['spec'],'name':'동일조건 두 번째 정책'}))
    p2=policies.review(w.reviewer,w.company,p2['id'],PolicyReview(expected_revision=p2['revision'],cases=[PolicyTruthCase(event_id=event['id'],expected_lines=EXPECTED)],reason='독립 정답 검증'))
    p2=policies.activate(w.owner,w.company,p2['id'],PolicyAction(expected_revision=p2['revision'],reason='시험'))
    engine=AutomaticBooks(w.svc,execution_enabled=True)
    g=consent(engine,w,p,policy_ids=[p['id'],p2['id']])
    with pytest.raises(DomainError) as e:engine.process(w.company,g['id'],event['id'])
    assert e.value.code=='AUTOMATION_POLICY_AMBIGUOUS'


def test_total_cap_does_not_reset_per_new_source(w):
    engine,g,event,_,work=ready(w,max_total=1100,max_per_event=1100)
    engine.process(w.company,g['id'],event['id'])
    second=INVOICES.replace(b'SYNTHETIC-01',b'SYNTHETIC-NEW')
    upload(w,second,key='auto-next-invoice',filename='다음매입.csv')
    Ingestion(w.svc).run_once(w.company);work.refresh(w.owner,w.company)
    events=[e for e in work.list(w.owner,w.company) if e['kind']=='invoice' and e['id']!=event['id']]
    assert len(events)==1
    e=events[0]
    answer(work,w,e,'business_use','business',key='auto-next-business')
    answer(work,w,e,'use_type','operations',key='auto-next-purpose')
    with pytest.raises(DomainError) as err:engine.process(w.company,g['id'],e['id'])
    assert err.value.code=='AUTOMATION_TOTAL_LIMIT'
    with w.db.connect() as c:assert len(w.db.list(c,w.company,'journal'))==1


def test_consent_requires_owner_and_cannot_cross_company(w):
    engine,g,event,_,_=ready(w)
    with pytest.raises(DomainError):engine.status(w.other_owner,w.company)
    with pytest.raises(DomainError):engine.stop(w.viewer,w.company,g['id'],AutomationAction(expected_revision=g['revision'],reason='viewer'))
    with pytest.raises(DomainError):engine.process(w.other_company,g['id'],event['id'])


def test_consent_is_payload_bound_and_parallel_grants_forbidden(w):
    engine,g,event,_,_=ready(w)
    data={**g['spec'],'idempotency_key':'consent-synthetic-01','confirmed':True}
    assert engine.consent(w.owner,w.company,AutomationConsent(**data))['id']==g['id']
    with pytest.raises(DomainError) as e:engine.consent(w.owner,w.company,AutomationConsent(**{**data,'max_events':21}))
    assert e.value.code=='AUTOMATION_KEY_CONFLICT'
    with pytest.raises(DomainError) as e:engine.consent(w.owner,w.company,AutomationConsent(**{**data,'idempotency_key':'another-grant-key'}))
    assert e.value.code=='AUTOMATION_ALREADY_ACTIVE'
