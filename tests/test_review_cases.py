from datetime import datetime,timedelta,timezone
import pytest
from price.review_cases import ReviewCases,ReviewRequest,ReviewAssignment,ReviewRevision,ReviewFinding
from price.models import AuthorityInput,UserInput
from price.auth import Actor
from price.db import DomainError
from test_casework import setup_sources,invoice,answer
from conftest import PASSWORD


def assigned(w):
    work=setup_sources(w);event=invoice(work,w);reviews=ReviewCases(w.svc)
    review=reviews.open(w.owner,w.company,ReviewRequest(event_id=event['id'],reason='거래 사실이 불명확하여 검토 요청',idempotency_key='review-request-01'))
    assert review['state']=='UNASSIGNED' and review['assignee'] is None
    review=reviews.assign(w.owner,w.company,review['id'],ReviewAssignment(expected_revision=review['revision'],reviewer_id=w.reviewer.id,due_at=datetime.now(timezone.utc)+timedelta(days=1)))
    return reviews,review,event,work


def test_assignment_is_not_accepted_review(w):
    reviews,r,event,work=assigned(w)
    assert r['state']=='ASSIGNED' and r['professional_credential_verified'] is False
    r=reviews.accept(w.reviewer,w.company,r['id'],ReviewRevision(expected_revision=r['revision'],reason='담당자 수락'))
    assert r['state']=='IN_REVIEW' and r['accepted_at']
    with pytest.raises(DomainError):reviews.accept(w.owner,w.company,r['id'],ReviewRevision(expected_revision=r['revision'],reason='자기 수락 금지'))


def test_fact_change_requires_new_review_context(w):
    reviews,r,event,work=assigned(w)
    answer(work,w,event,'business_use','business')
    with pytest.raises(DomainError) as error:reviews.accept(w.reviewer,w.company,r['id'],ReviewRevision(expected_revision=r['revision'],reason='낡은 질문'))
    assert error.value.code=='REVIEW_FACT_CHANGED'
    new=reviews.open(w.owner,w.company,ReviewRequest(event_id=event['id'],reason='변경 사실로 재검토',idempotency_key='review-request-02'))
    assert new['id']!=r['id']


def test_finding_is_independently_verified_without_posting(w):
    reviews,r,event,work=assigned(w)
    r=reviews.accept(w.reviewer,w.company,r['id'],ReviewRevision(expected_revision=r['revision'],reason='수락'))
    auth=w.svc.add_authority(w.owner,w.company,AuthorityInput(title='Synthetic review source',url='https://www.law.go.kr',effective_from='2026-01-01',effective_to='2026-12-31',excerpt='Synthetic reasoning fixture only.',kind='sample'))
    auth=w.svc.review_authority(w.reviewer,w.company,auth['id'],auth['revision'],'근거 비교')
    r=reviews.conclude(w.reviewer,w.company,r['id'],ReviewFinding(expected_revision=r['revision'],conclusion='합성 검토 결과; 실제 세무 자문 아님',authority_ids=[auth['id']],evidence_ids=[w.evidence]))
    assert r['state']=='FINDING_RECORDED'
    with pytest.raises(DomainError):reviews.verify(w.reviewer,w.company,r['id'],ReviewRevision(expected_revision=r['revision'],reason='자기 검증 금지'))
    new=w.svc.add_user(w.owner,w.company,UserInput(username='second-reviewer',password=PASSWORD,role='reviewer'))
    verifier=Actor(new['id'],new['username'])
    completed=reviews.verify(verifier,w.company,r['id'],ReviewRevision(expected_revision=r['revision'],reason='독립 비교'))
    assert completed['state']=='VERIFIED_FINDING' and not completed['accounting_posted']
    assert completed['professional_credential_verified'] is False
    with w.db.connect() as c:assert not w.db.list(c,w.company,'journal')


def test_review_tenant_role_and_stale_revision(w):
    reviews,r,event,work=assigned(w)
    with pytest.raises(DomainError):reviews.list(w.other_owner,w.company)
    with pytest.raises(DomainError):reviews.accept(w.viewer,w.company,r['id'],ReviewRevision(expected_revision=r['revision'],reason='권한 없음'))
    with pytest.raises(DomainError):reviews.accept(w.reviewer,w.company,r['id'],ReviewRevision(expected_revision=99,reason='버전 불일치'))


def test_unknown_answer_creates_real_unassigned_case_and_tracks_acceptance(w):
    work=setup_sources(w);event=invoice(work,w)
    saved=answer(work,w,event,'business_use','unknown')
    assert saved['event']['review_id'] and saved['event']['expert_assignee'] is None
    reviews=ReviewCases(w.svc);r=reviews.list(w.owner,w.company)[0]
    assert r['state']=='UNASSIGNED'
    r=reviews.assign(w.owner,w.company,r['id'],ReviewAssignment(expected_revision=r['revision'],reviewer_id=w.reviewer.id,due_at=datetime.now(timezone.utc)+timedelta(days=1)))
    assigned_event=invoice(work,w)
    assert assigned_event['state']=='REVIEW_ASSIGNED'
    assert assigned_event['expert_assignee']['user_id']==w.reviewer.id
    reviews.accept(w.reviewer,w.company,r['id'],ReviewRevision(expected_revision=r['revision'],reason='실제 검토 수락'))
    assert invoice(work,w)['state']=='IN_REVIEW'
    with w.db.transaction() as c:c.execute('UPDATE users SET disabled=1 WHERE id=?',(w.reviewer.id,))
    assert invoice(work,w)['state']=='REVIEW_CONNECTION_NEEDED'
