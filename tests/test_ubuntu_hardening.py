"""Additional Ubuntu integration regressions; retain separation through automation."""
import pytest
from price.db import DomainError
from price.models import FactsInput
from price.worker import Queue


def test_worker_does_not_erase_transaction_author_for_approval(w):
    tx=w.tx();q=Queue(w.svc);q.enqueue(w.owner,w.company,tx['id'])
    j=q.run_once(w.company)['result']['journal']
    with pytest.raises(DomainError) as err:
        w.svc.transition_journal(w.owner,w.company,j['id'],'approve',j['revision'])
    assert err.value.code=='SELF_APPROVAL'


def test_other_analyzer_does_not_allow_source_author_self_approval(w):
    tx=w.tx()
    j=w.svc.analyze(w.preparer,w.company,tx['id'])['journal']
    with pytest.raises(DomainError) as err:
        w.svc.transition_journal(w.owner,w.company,j['id'],'approve',j['revision'])
    assert err.value.code=='SELF_APPROVAL'


def test_fact_editor_must_not_approve_worker_generated_journal(w):
    tx=w.tx(business_purpose='')
    tx=w.svc.update_facts(w.reviewer,w.company,tx['id'],FactsInput(expected_revision=tx['revision'],business_purpose='검토자가 직접 기입한 거래 사실',tax_category='standard',evidence_ids=[w.evidence],reason='사실 편집'))
    q=Queue(w.svc);q.enqueue(w.owner,w.company,tx['id']);j=q.run_once(w.company)['result']['journal']
    with pytest.raises(DomainError) as err:
        w.svc.transition_journal(w.reviewer,w.company,j['id'],'approve',j['revision'])
    assert err.value.code=='SELF_APPROVAL'


def test_independent_reviewer_can_approve_worker_proposal(w):
    tx=w.tx();q=Queue(w.svc);q.enqueue(w.owner,w.company,tx['id']);j=q.run_once(w.company)['result']['journal']
    j=w.svc.transition_journal(w.reviewer,w.company,j['id'],'approve',j['revision'])
    j=w.svc.transition_journal(w.owner,w.company,j['id'],'post',j['revision'])
    assert j['state']=='POSTED'
