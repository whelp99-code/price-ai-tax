"""Actual source-backed subledger postings, not schedule-only simulations."""
from datetime import date
from concurrent.futures import ThreadPoolExecutor
import pytest
from price.book_schedules import BookSchedules,BookScheduleInput,ScheduleReview
from price.db import DomainError
from price.models import JournalInput,Line
from price.finance_ops import FinanceOps


def make_plan(w,kind='prepaid',**changes):
    source_date=changes.pop('source_date',date(2026,9,1))
    tx=w.tx(direction='purchase',net=1001,tax=100,gross=1101,deductible=True,event_date=source_date)
    journal=w.post(tx)
    data=dict(kind=kind,source_journal_id=journal['id'],label='합성 회사 연간 구독',classification_date=date(2026,9,1),start=date(2026,9,1),end=date(2026,11,30),evidence_ids=[w.evidence],idempotency_key='schedule-plan-test-01')
    if kind=='asset':data.update(end=None,months=3,salvage=101)
    data.update(changes)
    service=BookSchedules(w.svc,today=lambda:date(2026,12,31))
    plan=service.propose(w.owner,w.company,BookScheduleInput(**data))
    return service,plan,journal


def approved(w,kind='prepaid',**changes):
    service,plan,journal=make_plan(w,kind,**changes)
    plan=service.approve(w.reviewer,w.company,plan['id'],ScheduleReview(expected_revision=plan['revision'],reason='원본 및 전체 인식 일정 독립 검토'))
    return service,plan,journal


def test_real_prepaid_postings_conserve_expense_and_original_vat(w):
    service,plan,original=approved(w)
    assert plan['state']=='APPROVED' and plan['cost']==1001
    first=service.post_due(w.owner,w.company,plan['id'],date(2026,9,30))
    assert len(first['created_journal_ids'])==2
    last=service.post_due(w.owner,w.company,plan['id'],date(2026,11,30))
    assert len(last['created_journal_ids'])==2 and last['state']=='COMPLETE'
    assert service.post_due(w.owner,w.company,plan['id'],date(2026,11,30))['created_journal_ids']==[]
    with w.db.connect() as c:
        journals=w.db.list(c,w.company,'journal')
        assert len(journals)==5
        assert w.db.get(c,w.company,'journal',original['id'])==original
        assert all(j['state']=='POSTED' for j in journals)
        entries=[line for j in journals for line in j['lines']]
        net=lambda account:sum(l['amount']*(1 if l['side']=='debit' else -1) for l in entries if l['account']==account)
        assert net('expense')==1001 and net('prepaid_expense')==0 and net('vat_input')==100
        assert w.db.verify_audit(c,w.company)['valid']
    report=FinanceOps(w.svc).report(w.owner,w.company,date(2026,9,1),date(2026,9,30))
    assert report['profit']['expenses']==330


def test_asset_reclassification_depreciation_and_subledger(w):
    service,plan,original=approved(w,'asset')
    service.post_due(w.owner,w.company,plan['id'],date(2026,11,30))
    item=service.get(w.owner,w.company,plan['id'])
    assert item['recognized_expense']==900 and item['book_value']==101
    assert item['tax_depreciation_verified'] is False
    with w.db.connect() as c:
        journals=w.db.list(c,w.company,'journal')
        entries=[l for j in journals for l in j['lines']]
        assert sum(l['amount'] for l in entries if l['account']=='depreciation_expense' and l['side']=='debit')==900
        assert sum(l['amount'] for l in entries if l['account']=='accumulated_depreciation' and l['side']=='credit')==900
        assert not any(l['account']=='cash' for j in journals if j['id']!=original['id'] for l in j['lines'])
    report=FinanceOps(w.svc).report(w.owner,w.company,date(2026,9,1),date(2026,11,30))
    assert report['profit']['expenses']==900


def test_no_self_approval_no_unapproved_execution(w):
    service,plan,_=make_plan(w)
    with pytest.raises(DomainError) as e:service.approve(w.owner,w.company,plan['id'],ScheduleReview(expected_revision=plan['revision'],reason='자기 승인'))
    assert e.value.code=='SELF_APPROVAL'
    with pytest.raises(DomainError):service.post_due(w.owner,w.company,plan['id'],date(2026,9,30))


def test_plan_is_idempotent_and_same_source_cannot_be_consumed_twice(w):
    service,plan,original=make_plan(w)
    assert service.propose(w.owner,w.company,BookScheduleInput(**plan['input']))['id']==plan['id']
    different={**plan['input'],'idempotency_key':'second-plan-same-source'}
    with pytest.raises(DomainError) as e:service.propose(w.owner,w.company,BookScheduleInput(**different))
    assert e.value.code=='SOURCE_ALREADY_SCHEDULED'


def test_changed_approved_schedule_is_rejected(w):
    service,plan,_=approved(w)
    with w.db.transaction() as c:
        stored=w.db.get(c,w.company,'book_schedule',plan['id']);stored['entries'][0]['lines'][0]['amount']+=1
        w.db.replace(c,w.company,'book_schedule',plan['id'],stored,stored['revision'])
    with pytest.raises(DomainError) as e:service.post_due(w.owner,w.company,plan['id'],date(2026,9,30))
    assert e.value.code=='SCHEDULE_APPROVAL_CHANGED'


def test_reviewer_revocation_immediately_stops_later_postings(w):
    service,plan,_=approved(w)
    service.post_due(w.owner,w.company,plan['id'],date(2026,9,1))
    with w.db.transaction() as c:c.execute('DELETE FROM memberships WHERE company_id=? AND user_id=?',(w.company,w.reviewer.id))
    with pytest.raises(DomainError):service.post_due(w.owner,w.company,plan['id'],date(2026,9,30))
    assert service.get(w.owner,w.company,plan['id'])['recognized_expense']==0


def test_period_lock_rolls_back_whole_due_batch(w):
    service,plan,_=approved(w)
    from price.models import PeriodInput
    period=w.svc.create_period(w.owner,w.company,PeriodInput(label='잠금 시험',start=date(2026,9,30),end=date(2026,9,30),required_sources=['manual']))
    with w.db.transaction() as c:w.db.replace(c,w.company,'period',period['id'],{**period,'closed':True},period['revision'])
    with pytest.raises(DomainError) as e:service.post_due(w.owner,w.company,plan['id'],date(2026,9,30))
    assert e.value.code=='PERIOD_LOCKED'
    with w.db.connect() as c:assert len(w.db.list(c,w.company,'journal'))==1


def test_concurrent_due_execution_has_one_ledger_effect(w):
    service,plan,_=approved(w)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:service.post_due(w.owner,w.company,plan['id'],date(2026,11,30)),range(2)))
    assert sum(len(x['created_journal_ids']) for x in results)==4
    with w.db.connect() as c:assert len(w.db.list(c,w.company,'journal'))==5


def test_source_reversal_blocks_pending_schedule(w):
    service,plan,original=approved(w)
    rev=w.svc.reverse_journal(w.owner,w.company,original['id'],'2026-09-02','원구매 취소',original['revision'])
    rev=w.svc.transition_journal(w.owner,w.company,rev['id'],'validate',rev['revision'])
    rev=w.svc.transition_journal(w.reviewer,w.company,rev['id'],'approve',rev['revision'],'독립 확인')
    w.svc.transition_journal(w.owner,w.company,rev['id'],'post',rev['revision'])
    with pytest.raises(DomainError) as e:service.post_due(w.owner,w.company,plan['id'],date(2026,9,30))
    assert e.value.code=='SCHEDULE_SOURCE_REVERSED'


def test_cancel_after_partial_keeps_posted_entries_and_remainder(w):
    service,plan,_=approved(w)
    service.post_due(w.owner,w.company,plan['id'],date(2026,9,30))
    current=service.get(w.owner,w.company,plan['id'])
    cancelled=service.cancel(w.owner,w.company,plan['id'],ScheduleReview(expected_revision=current['revision'],reason='계약 조기 종료 확인'))
    assert cancelled['state']=='CANCELLED' and cancelled['book_value']==671
    with pytest.raises(DomainError):service.post_due(w.owner,w.company,plan['id'],date(2026,11,30))
    with w.db.connect() as c:assert len(w.db.list(c,w.company,'journal'))==3


def test_future_date_and_cross_company_blocked(w):
    service,plan,_=approved(w)
    with pytest.raises(DomainError):service.post_due(w.other_owner,w.company,plan['id'],date(2026,9,30))
    with pytest.raises(DomainError):service.post_due(w.owner,w.company,plan['id'],date(9999,12,31))


def test_zero_depreciable_asset_posts_reclass_without_zero_lines(w):
    service,plan,_=approved(w,'asset',salvage=1001)
    result=service.post_due(w.owner,w.company,plan['id'],date(2026,11,30))
    assert len(result['created_journal_ids'])==1 and result['state']=='COMPLETE'
    assert result['book_value']==1001
