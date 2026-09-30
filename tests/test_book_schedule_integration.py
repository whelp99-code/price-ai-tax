from datetime import date
import pytest
from fastapi.testclient import TestClient
from price.api import create_app
from price.book_schedules import BookSchedules,ScheduleReview
from price.models import PeriodInput,CoverageInput
from price.db import DomainError
from test_book_schedules import approved,make_plan
from conftest import PASSWORD


def test_due_unposted_schedule_blocks_period_close(w):
    service,plan,_=approved(w)
    p=w.svc.create_period(w.owner,w.company,PeriodInput(label='2026-09',start=date(2026,9,1),end=date(2026,9,30)))
    p=w.svc.coverage(w.reviewer,w.company,p['id'],CoverageInput(expected_revision=p['revision'],opening_reviewed=True,sources_confirmed=['manual'],evidence_note='합성 원장 기초 및 수집범위 대조'))
    readiness=w.svc.readiness(w.owner,w.company,p['id'])
    assert any(x['code']=='SCHEDULE_NOT_POSTED' for x in readiness['blockers'])
    service.post_due(w.owner,w.company,plan['id'],date(2026,9,30))
    assert not any(x['code']=='SCHEDULE_NOT_POSTED' for x in w.svc.readiness(w.owner,w.company,p['id'])['blockers'])


def test_schedule_changes_invalidate_tax_dependency_snapshot(w):
    service,plan,_=make_plan(w)
    p=w.period()
    with w.db.connect() as c:before=w.svc._snapshot(c,w.company,p)
    service.approve(w.reviewer,w.company,plan['id'],ScheduleReview(expected_revision=plan['revision'],reason='월별 원가 인식 확인'))
    with w.db.connect() as c:after=w.svc._snapshot(c,w.company,p)
    assert before!=after


def test_worker_posts_only_approved_due_entries(w):
    service,plan,_=make_plan(w)
    assert service.run_once(w.company)['processed'] is False
    service.approve(w.reviewer,w.company,plan['id'],ScheduleReview(expected_revision=plan['revision'],reason='원가 확인'))
    assert service.run_once(w.company)['state']=='POSTED'
    assert service.run_once(w.company)['processed'] is False


def test_reversed_schedule_expense_blocks_further_posting_and_corrects_display(w):
    service,plan,_=approved(w)
    output=service.post_due(w.owner,w.company,plan['id'],date(2026,9,30))
    with w.db.connect() as c:
        original=next(j for j in w.db.list(c,w.company,'journal') if j.get('book_schedule_role')=='PERIOD_EXPENSE')
    rev=w.svc.reverse_journal(w.owner,w.company,original['id'],'2026-10-01','일정 변경 필요',original['revision'])
    rev=w.svc.transition_journal(w.owner,w.company,rev['id'],'validate',rev['revision'])
    rev=w.svc.transition_journal(w.reviewer,w.company,rev['id'],'approve',rev['revision'],'독립 확인')
    w.svc.transition_journal(w.owner,w.company,rev['id'],'post',rev['revision'])
    view=service.get(w.owner,w.company,plan['id'])
    assert view['recognized_expense']==0 and view['reversed_journal_ids']==[original['id']]
    with pytest.raises(DomainError) as e:service.post_due(w.owner,w.company,plan['id'],date(2026,11,30))
    assert e.value.code=='SCHEDULE_ENTRY_REVERSED'


def test_failed_plan_does_not_prevent_another_approved_schedule(w):
    service,first,_=approved(w)
    second_source=w.post(w.tx(direction='purchase',net=100,tax=10,gross=110,deductible=True,event_date=date(2026,9,2),external_id='another-expense'))
    from price.book_schedules import BookScheduleInput
    second=service.propose(w.owner,w.company,BookScheduleInput(kind='asset',source_journal_id=second_source['id'],label='둘째 자산',classification_date=date(2026,9,2),start=date(2026,9,2),months=2,salvage=0,evidence_ids=[w.evidence],idempotency_key='second-asset-plan'))
    second=service.approve(w.reviewer,w.company,second['id'],ScheduleReview(expected_revision=second['revision'],reason='두번째 계획 확인'))
    with w.db.transaction() as c:
        p=w.db.get(c,w.company,'book_schedule',first['id']);p['approval_hash']='broken';w.db.replace(c,w.company,'book_schedule',p['id'],p,p['revision'])
    results=[service.run_once(w.company) for _ in range(3)]
    assert any(x.get('state')=='POSTED' for x in results)
    assert service.get(w.owner,w.company,second['id'])['remaining_entries']==0


def test_api_owner_reads_book_values_without_account_fields(w):
    service,plan,_=make_plan(w)
    app=create_app(w.db.path)
    with TestClient(app) as client:
        response=client.post('/api/login',json={'username':w.owner.username,'password':PASSWORD})
        client.headers['x-csrf-token']=response.json()['csrf']
        prefix='/api/companies/'+w.company
        data=client.get(prefix+'/owner/book-schedules')
        assert data.status_code==200,data.text
        assert data.json()['items'][0]['label']=='합성 회사 연간 구독'
        assert 'input' not in data.json()['items'][0]
        attempt=client.post(prefix+'/book-schedules/'+plan['id']+'/approve',json={'expected_revision':plan['revision'],'reason':'내 승인'})
        assert attempt.status_code==403
