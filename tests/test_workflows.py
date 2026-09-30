import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from price.auth import Actor
from price.db import DomainError
from price.models import *
from price.filing import Filings
from price.worker import Queue


def test_supported_transaction_analysis_reused_not_posted(w):
    tx=w.tx(); r=w.analyze(tx); again=w.analyze(tx)
    assert r['case']['state']=='PROPOSED'
    assert r['journal']['state']=='VALIDATED'
    assert again['reused'] and again['journal']['id']==r['journal']['id']
    assert w.svc.trial_balance(w.owner,w.company)['debit']==0

@pytest.mark.parametrize('changes,state', [(dict(business_purpose=''),'NEEDS_FACTS'),(dict(evidence_ids=[]),'NEEDS_FACTS'),(dict(direction='purchase',deductible=None),'NEEDS_FACTS'),(dict(currency='USD'),'BLOCKED_SCOPE'),(dict(related_party=True),'BLOCKED_SCOPE'),(dict(use_type='asset'),'BLOCKED_SCOPE')])
def test_missing_or_unsupported_never_silently_posts(w,changes,state):
    r=w.analyze(w.tx(**changes))
    assert r['case']['state']==state and r['journal'] is None

def test_self_approval_and_skipped_state_rejected(w):
    j=w.analyze(w.tx())['journal']
    with pytest.raises(DomainError) as e: w.svc.transition_journal(w.owner,w.company,j['id'],'approve',j['revision'])
    assert e.value.code=='SELF_APPROVAL'
    with pytest.raises(DomainError): w.svc.transition_journal(w.owner,w.company,j['id'],'post',j['revision'])
    with pytest.raises(DomainError): w.svc.transition_journal(w.preparer,w.company,j['id'],'approve',j['revision'])

def test_stale_revision_and_exact_approved_post(w):
    j=w.analyze(w.tx())['journal']
    with pytest.raises(DomainError) as e: w.svc.transition_journal(w.reviewer,w.company,j['id'],'approve',1)
    assert e.value.code=='STALE_VERSION'
    j=w.svc.transition_journal(w.reviewer,w.company,j['id'],'approve',j['revision'])
    j=w.svc.transition_journal(w.owner,w.company,j['id'],'post',j['revision'])
    assert j['state']=='POSTED' and w.svc.trial_balance(w.owner,w.company)['balanced']
    with pytest.raises(DomainError): w.svc.transition_journal(w.owner,w.company,j['id'],'post',j['revision'])
    with pytest.raises(sqlite3.IntegrityError):
        with w.db.transaction() as c: c.execute("UPDATE objects SET body='{}' WHERE company_id=? AND kind='journal' AND id=?",(w.company,j['id']))

def test_approved_source_fact_change_invalidates_draft(w):
    tx=w.tx(); j=w.analyze(tx)['journal']
    j=w.svc.transition_journal(w.reviewer,w.company,j['id'],'approve',j['revision'])
    w.svc.update_facts(w.owner,w.company,tx['id'],FactsInput(expected_revision=tx['revision'],business_purpose='수정 목적',tax_category='standard',evidence_ids=[w.evidence],reason='새 사실'))
    with pytest.raises(DomainError): w.svc.transition_journal(w.owner,w.company,j['id'],'post',j['revision'])
    assert w.svc.get_object(w.owner,w.company,'journal',j['id'])['state']=='SUPERSEDED'

def test_approval_expired_or_reviewer_revoked(w):
    j=w.analyze(w.tx())['journal']; j=w.svc.transition_journal(w.reviewer,w.company,j['id'],'approve',j['revision'])
    with w.db.transaction() as c: c.execute('UPDATE users SET disabled=1 WHERE id=?',(w.reviewer.id,))
    with pytest.raises(DomainError): w.svc.transition_journal(w.owner,w.company,j['id'],'post',j['revision'])
    with w.db.transaction() as c:
        c.execute('UPDATE users SET disabled=0 WHERE id=?',(w.reviewer.id,))
        j=w.db.replace(c,w.company,'journal',j['id'],j|{'approval_expires':int(time.time())-1},j['revision'])
    with pytest.raises(DomainError) as e: w.svc.transition_journal(w.owner,w.company,j['id'],'post',j['revision'])
    assert e.value.code=='INVALID_APPROVAL'

def test_manual_journal_cannot_bypass_missing_facts(w):
    tx=w.tx(business_purpose='')
    j=w.svc.create_journal(w.owner,w.company,JournalInput(transaction_id=tx['id'],event_date=tx['event_date'],description='attempt',evidence_ids=[w.evidence],lines=[Line(account='receivable',side='debit',amount=1100),Line(account='revenue',side='credit',amount=1000),Line(account='vat_output',side='credit',amount=100)]))
    with pytest.raises(DomainError) as e: w.svc.transition_journal(w.owner,w.company,j['id'],'validate',j['revision'])
    assert e.value.code=='SOURCE_FACTS_UNRESOLVED'

def test_manual_tax_without_transaction_blocked(w):
    j=w.svc.create_journal(w.owner,w.company,JournalInput(event_date='2026-09-12',description='attempt',evidence_ids=[w.evidence],lines=[Line(account='vat_input',side='debit',amount=100),Line(account='cash',side='credit',amount=100)]))
    with pytest.raises(DomainError) as e: w.svc.transition_journal(w.owner,w.company,j['id'],'validate',j['revision'])
    assert e.value.code=='TAX_SOURCE_REQUIRED'

def test_reversal_preserves_original_and_balances(w):
    original=w.post(w.tx())
    reverse=w.svc.reverse_journal(w.owner,w.company,original['id'],'2026-09-20','시험 취소',original['revision'])
    for actor,action in [(w.owner,'validate'),(w.reviewer,'approve'),(w.owner,'post')]:
        reverse=w.svc.transition_journal(actor,w.company,reverse['id'],action,reverse['revision'])
    assert w.svc.get_object(w.owner,w.company,'journal',original['id'])==original
    assert all(row['net_debit']==0 for row in w.svc.trial_balance(w.owner,w.company)['rows'])
    with pytest.raises(DomainError): w.svc.reverse_journal(w.owner,w.company,original['id'],'2026-09-20','twice',original['revision'])
    p=w.closed_period(); calc=w.svc.calculate(w.owner,w.company,CalculationInput(period_id=p['id']))
    assert calc['totals']['net_vat']==0

def test_partial_and_full_reconciliation(w):
    sale=w.tx(); bank=w.tx(kind='bank',direction='receipt',net=1100,tax=0,gross=1100)
    w.svc.allocate(w.owner,w.company,AllocationInput(bank_id=bank['id'],invoice_id=sale['id'],amount=500,reason='부분 수금'))
    assert w.analyze(bank)['case']['state']=='NEEDS_FACTS'
    with pytest.raises(DomainError): w.svc.allocate(w.owner,w.company,AllocationInput(bank_id=bank['id'],invoice_id=sale['id'],amount=601,reason='초과'))
    w.svc.allocate(w.owner,w.company,AllocationInput(bank_id=bank['id'],invoice_id=sale['id'],amount=600,reason='잔여 수금'))
    assert w.analyze(bank)['journal']['state']=='VALIDATED'

def test_reconciliation_wrong_direction(w):
    sale=w.tx(); payment=w.tx(kind='bank',direction='payment',net=1100,tax=0,gross=1100)
    with pytest.raises(DomainError): w.svc.allocate(w.owner,w.company,AllocationInput(bank_id=payment['id'],invoice_id=sale['id'],amount=100,reason='wrong'))

def test_empty_unverified_period_is_not_ready(w):
    p=w.period(); r=w.svc.readiness(w.owner,w.company,p['id'])
    assert not r['ready'] and not r['official_tax_ready']
    with pytest.raises(DomainError): w.svc.period_action(w.owner,w.company,p['id'],'close',p['revision'],'attempt')

def test_unsupported_transaction_blocks_complete_period(w):
    w.analyze(w.tx(currency='USD'))
    p=w.period(); p=w.svc.coverage(w.owner,w.company,p['id'],CoverageInput(expected_revision=p['revision'],opening_reviewed=True,sources_confirmed=['manual'],evidence_note='기초 확인'))
    r=w.svc.readiness(w.owner,w.company,p['id'])
    assert any(x['code']=='OPEN_CASE' for x in r['blockers']) and not r['ready']

def test_closed_period_prevents_new_entry_and_coverage_edit(w):
    p=w.closed_period()
    with pytest.raises(DomainError) as e: w.tx()
    assert e.value.code=='PERIOD_LOCKED'
    with pytest.raises(DomainError): w.svc.coverage(w.owner,w.company,p['id'],CoverageInput(expected_revision=p['revision'],opening_reviewed=True,sources_confirmed=['manual'],evidence_note='attempt'))

def test_vat_only_posted_and_trace_not_official_return(w):
    w.post(w.tx()); w.post(w.tx(direction='purchase',net=300,tax=30,gross=330,deductible=True))
    pending=w.tx(net=9000,tax=900,gross=9900);w.analyze(pending)
    p=w.period(); calc=w.svc.calculate(w.owner,w.company,CalculationInput(period_id=p['id']))
    assert calc['totals']=={'sales_net':1000,'output_vat':100,'input_vat':30,'net_vat':70}
    assert calc['trace'] and calc['blockers'] and calc['review_only'] and not calc['official_form']

@pytest.mark.parametrize('tax_type',['CORPORATE','WITHHOLDING'])
def test_unsupported_tax_does_not_return_invented_calculation(w,tax_type):
    with pytest.raises(DomainError) as e: w.svc.calculate(w.owner,w.company,CalculationInput(period_id='x',tax_type=tax_type))
    assert e.value.code=='TAX_RULEPACK_NOT_VERIFIED'

def test_live_submission_always_blocked(w):
    engine,f,_=w.ready_filing()
    with pytest.raises(DomainError) as e: engine.submit(w.reviewer,w.company,f['id'],SubmitInput(expected_revision=f['revision'],environment='production'))
    assert e.value.code=='LIVE_CHANNEL_UNCONFIGURED'
    assert not w.svc.list_objects(w.owner,w.company,'submission')

def test_filing_timeout_query_first_and_receipt_distinct_from_payment(w):
    engine,f,_=w.ready_filing(); revision=f['revision']
    r=engine.submit(w.reviewer,w.company,f['id'],SubmitInput(expected_revision=revision,scenario='timeout_after_accept'))
    assert r['filing']['state']=='SUBMISSION_UNKNOWN' and r['external_requests']==0
    with pytest.raises(DomainError) as e: engine.submit(w.reviewer,w.company,f['id'],SubmitInput(expected_revision=revision))
    assert e.value.code=='RECONCILE_REQUIRED'
    f=engine.reconcile(w.reviewer,w.company,f['id'])['filing']
    assert f['state']=='SIMULATED_RECEIVED' and f['receipt']['receipt_no'].startswith('SIM-') and not f['receipt']['is_real_filing']
    payment=engine.record_payment(w.owner,w.company,f['id'],PaymentInput(amount=100,evidence_id=w.evidence,note='모의 납부 기록'))
    assert not payment['bank_verified']
    assert w.svc.get_object(w.owner,w.company,'filing',f['id'])['payment_status']=='UNCONFIRMED'
    with pytest.raises(DomainError): engine.record_payment(w.owner,w.company,f['id'],PaymentInput(amount=100,evidence_id=w.evidence,note='duplicate'))
    replay=engine.submit(w.reviewer,w.company,f['id'],SubmitInput(expected_revision=revision))
    assert replay['replayed'] and len(w.svc.list_objects(w.owner,w.company,'submission'))==1

def test_sandbox_rejection_is_not_received(w):
    engine,f,_=w.ready_filing();r=engine.submit(w.reviewer,w.company,f['id'],SubmitInput(expected_revision=f['revision'],scenario='reject'))
    assert r['filing']['state']=='SIMULATED_REJECTED'
    with pytest.raises(DomainError): engine.record_payment(w.owner,w.company,f['id'],PaymentInput(amount=1,evidence_id=w.evidence,note='attempt'))

def test_reopened_period_invalidates_filing_approval(w):
    engine,f,p=w.ready_filing()
    w.svc.period_action(w.owner,w.company,p['id'],'reopen',p['revision'],'원본 재검토')
    with pytest.raises(DomainError) as e: engine.submit(w.reviewer,w.company,f['id'],SubmitInput(expected_revision=f['revision']))
    assert e.value.code=='FILING_BASIS_CHANGED'

def test_concurrent_submissions_never_create_duplicate_attempt(w):
    engine,f,_=w.ready_filing()
    def call():
        try: return engine.submit(w.reviewer,w.company,f['id'],SubmitInput(expected_revision=f['revision']))
        except DomainError as e:return {'blocked':e.code}
    with ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(lambda _:call(),range(4)))
    assert len(w.svc.list_objects(w.owner,w.company,'submission'))==1
    assert any(r.get('filing',{}).get('state')=='SIMULATED_RECEIVED' for r in results)

def test_worker_proposes_once_never_posts(w):
    tx=w.tx();q=Queue(w.svc)
    a=q.enqueue(w.owner,w.company,tx['id']);b=q.enqueue(w.owner,w.company,tx['id']);assert a['id']==b['id']
    r=q.run_once(w.company);assert r['state']=='SUCCEEDED' and r['result']['journal']['state']=='VALIDATED'
    assert not q.run_once(w.company)['processed']
    j=r['result']['journal']
    with pytest.raises(DomainError): w.svc.transition_journal(Actor('system:price','price',worker=True),w.company,j['id'],'approve',j['revision'])

def test_worker_atomic_input_revision_guard(w):
    tx=w.tx();q=Queue(w.svc);q.enqueue(w.owner,w.company,tx['id'])
    w.svc.update_facts(w.owner,w.company,tx['id'],FactsInput(expected_revision=tx['revision'],business_purpose='변경',tax_category='standard',evidence_ids=[w.evidence],reason='changed'))
    r=q.run_once(w.company);assert r['state']=='BLOCKED' and r['error']=='STALE_JOB'
    with pytest.raises(DomainError):w.svc.analyze(w.owner,w.company,tx['id'],expected_revision=tx['revision'])
    assert not w.svc.list_objects(w.owner,w.company,'journal')

def test_exhausted_worker_lease_becomes_failed(w):
    tx=w.tx();q=Queue(w.svc);job=q.enqueue(w.owner,w.company,tx['id'])
    with w.db.transaction() as c:c.execute("UPDATE jobs SET state='RUNNING',lease_until=1,attempts=3 WHERE id=?",(job['id'],))
    assert not q.run_once(w.company)['processed']
    assert q.list(w.owner,w.company)[0]['state']=='FAILED'

def test_fact_history_retains_old_values_and_is_company_scoped(w):
    tx=w.tx(business_purpose='최초 확인 사실')
    w.svc.update_facts(w.owner,w.company,tx['id'],FactsInput(expected_revision=tx['revision'],business_purpose='수정 확인 사실',tax_category='standard',evidence_ids=[w.evidence],reason='원본 확인 정정'))
    r=w.svc.object_history(w.owner,w.company,'transaction',tx['id'])
    assert [x['business_purpose'] for x in r['revisions']]==['최초 확인 사실','수정 확인 사실']
    assert r['current_revision']==2
    with pytest.raises(DomainError):w.svc.object_history(w.other_owner,w.other_company,'transaction',tx['id'])
    with pytest.raises(sqlite3.IntegrityError):
        with w.db.transaction() as c:c.execute('DELETE FROM object_history')

def test_unreviewed_bank_source_exposes_scope_blocker(w):
    p=w.period();w.tx(source='unconfirmed-bank')
    r=w.svc.readiness(w.owner,w.company,p['id'])
    assert 'UNDECLARED_SOURCES' in {x['code'] for x in r['blockers']}

def test_approved_journal_digest_detects_tampering(w):
    j=w.analyze(w.tx())['journal'];j=w.svc.transition_journal(w.reviewer,w.company,j['id'],'approve',j['revision'])
    with w.db.transaction() as c:j=w.db.replace(c,w.company,'journal',j['id'],j|{'description':'tampered'},j['revision'])
    with pytest.raises(DomainError) as e:w.svc.transition_journal(w.owner,w.company,j['id'],'post',j['revision'])
    assert e.value.code=='INVALID_APPROVAL'

def test_concurrent_analysis_has_one_proposal(w):
    tx=w.tx()
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:w.analyze(tx),range(4)))
    assert len({r['journal']['id'] for r in results})==1
    assert len(w.svc.list_objects(w.owner,w.company,'journal'))==1
