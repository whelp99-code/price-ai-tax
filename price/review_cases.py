"""Real user-assigned review workflow; a database role is not a professional license.

Assignments, acceptance, findings and independent verification have separate
states. Nothing here posts a journal or pretends an unassigned review is active.
"""
from __future__ import annotations
from datetime import datetime,timezone
from typing import Annotated
from pydantic import Field,StrictInt,StrictStr
from .auth import Actor
from .db import DomainError,digest
from .casework import Casework
from .upload_models import UploadInput

class ReviewRequest(UploadInput):
    event_id:StrictStr
    reason:Annotated[StrictStr,Field(min_length=1,max_length=1000)]
    idempotency_key:Annotated[StrictStr,Field(pattern=r'^[A-Za-z0-9_-]{8,120}$')]

class ReviewAssignment(UploadInput):
    expected_revision:Annotated[StrictInt,Field(ge=1)]
    reviewer_id:StrictStr
    due_at:datetime

class ReviewFinding(UploadInput):
    expected_revision:Annotated[StrictInt,Field(ge=1)]
    conclusion:Annotated[StrictStr,Field(min_length=1,max_length=4000)]
    authority_ids:Annotated[list[StrictStr],Field(min_length=1,max_length=20)]
    evidence_ids:Annotated[list[StrictStr],Field(min_length=1,max_length=30)]

class ReviewRevision(UploadInput):
    expected_revision:Annotated[StrictInt,Field(ge=1)]
    reason:Annotated[StrictStr,Field(min_length=1,max_length=1000)]


def ensure_review_case(db,c,company,event,fact_hash,requester,reason):
    ident='review_'+digest({'company':company,'event':event['id'],'context':event['context_hash'],'facts':fact_hash})
    if c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='expert_review' AND id=?",(company,ident)).fetchone():
        return db.get(c,company,'expert_review',ident)
    value={'event_id':event['id'],'source_context_hash':event['context_hash'],'fact_context_hash':fact_hash,'reason':reason,'requester':requester,
           'state':'UNASSIGNED','assignee':None,'due_at':None,'conclusion':None,'professional_credential_verified':False,'accounting_posted':False}
    record=db.create(c,company,'expert_review',value,ident=ident)
    db.audit(c,company,requester,'review.opened',ident,{'event_id':event['id'],'automatically_created':True})
    return record


class ReviewCases:
    def __init__(self,service):self.service,self.db,self.auth,self.work=service,service.db,service.auth,Casework(service)

    def open(self,actor,company,data:ReviewRequest):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','preparer'))
            event=self.work._fresh_event(c,company,data.event_id)
            fact_hash=digest(self.work._answers(c,company,event))
            ident='review_'+digest({'company':company,'event':event['id'],'context':event['context_hash'],'facts':fact_hash})
            if c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='expert_review' AND id=?",(company,ident)).fetchone():return self.db.get(c,company,'expert_review',ident)
            value={'event_id':event['id'],'source_context_hash':event['context_hash'],'fact_context_hash':fact_hash,'reason':data.reason,'requester':actor.id,
                   'state':'UNASSIGNED','assignee':None,'due_at':None,'conclusion':None,'professional_credential_verified':False,'accounting_posted':False}
            row=self.db.create(c,company,'expert_review',value,ident=ident);self.db.audit(c,company,actor.id,'review.opened',ident)
            return row

    def _fresh(self,c,company,ident,expected):
        review=self.db.get(c,company,'expert_review',ident)
        if review['revision']!=expected:raise DomainError('STALE_VERSION','검토 상태가 바뀌었습니다.')
        event=self.work._fresh_event(c,company,review['event_id'])
        if review['source_context_hash']!=event['context_hash']:raise DomainError('REVIEW_SOURCE_CHANGED','검토 대상 원본이 바뀌었습니다.')
        if review['fact_context_hash']!=digest(self.work._answers(c,company,event)):raise DomainError('REVIEW_FACT_CHANGED','검토 중 업무 사실이 바뀌었습니다.')
        return review

    def assign(self,actor,company,ident,data:ReviewAssignment):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner',))
            review=self._fresh(c,company,ident,data.expected_revision)
            self.auth.guard(c,Actor(data.reviewer_id,'assigned-reviewer'),company,('reviewer',))
            if data.reviewer_id==review['requester']:raise DomainError('SELF_APPROVAL','요청자와 검토 담당자를 분리해야 합니다.',403)
            if review['state'] not in ('UNASSIGNED','ASSIGNED','DECLINED'):raise DomainError('REVIEW_STATE','이미 시작된 검토는 이 경로로 재지정할 수 없습니다.')
            if data.due_at.tzinfo is None or data.due_at<=datetime.now(timezone.utc):raise DomainError('REVIEW_DEADLINE','시간대가 있는 미래 검토 기한이 필요합니다.',422)
            row=self.db.replace(c,company,'expert_review',ident,{**review,'state':'ASSIGNED','assignee':data.reviewer_id,'due_at':data.due_at.isoformat()},review['revision'])
            self.db.audit(c,company,actor.id,'review.assigned',ident,{'reviewer':data.reviewer_id,'accepted':False})
            return row

    def accept(self,actor,company,ident,data:ReviewRevision):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('reviewer',))
            review=self._fresh(c,company,ident,data.expected_revision)
            if review['state']!='ASSIGNED' or review['assignee']!=actor.id:raise DomainError('REVIEW_NOT_ASSIGNED','본인에게 배정된 검토만 수락할 수 있습니다.',403)
            row=self.db.replace(c,company,'expert_review',ident,{**review,'state':'IN_REVIEW','accepted_at':datetime.now(timezone.utc).isoformat()},review['revision'])
            self.db.audit(c,company,actor.id,'review.accepted',ident)
            return row

    def conclude(self,actor,company,ident,data:ReviewFinding):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('reviewer',))
            review=self._fresh(c,company,ident,data.expected_revision)
            if review['state']!='IN_REVIEW' or review['assignee']!=actor.id:raise DomainError('REVIEW_NOT_ACCEPTED','수락한 담당자만 검토 결과를 기록할 수 있습니다.',403)
            self.service._evidence_check(c,company,data.evidence_ids,required=True)
            authorities=[]
            for ident_a in data.authority_ids:
                a=self.db.get(c,company,'authority',ident_a)
                if not a.get('reviewed'):raise DomainError('REVIEW_AUTHORITY','검토된 판단 근거가 필요합니다.')
                authorities.append({'id':a['id'],'revision':a['revision'],'hash':digest(a)})
            row=self.db.replace(c,company,'expert_review',ident,{**review,'state':'FINDING_RECORDED','conclusion':data.conclusion,'authority_refs':authorities,'evidence_ids':data.evidence_ids,'finding_hash':digest(data.model_dump())},review['revision'])
            self.db.audit(c,company,actor.id,'review.finding_recorded',ident,{'finding_hash':row['finding_hash']})
            return row

    def verify(self,actor,company,ident,data:ReviewRevision):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('reviewer',))
            review=self._fresh(c,company,ident,data.expected_revision)
            if review['state']!='FINDING_RECORDED' or actor.id in (review['assignee'],review['requester']):raise DomainError('REVIEW_INDEPENDENCE','판단 작성자·요청자와 다른 검토자가 확인해야 합니다.',403)
            for ref in review['authority_refs']:
                a=self.db.get(c,company,'authority',ref['id'])
                if a['revision']!=ref['revision'] or digest(a)!=ref['hash']:raise DomainError('REVIEW_AUTHORITY_CHANGED','근거가 변경되어 재검토가 필요합니다.')
            row=self.db.replace(c,company,'expert_review',ident,{**review,'state':'VERIFIED_FINDING','verified_by':actor.id,'verification_note':data.reason},review['revision'])
            self.db.audit(c,company,actor.id,'review.finding_verified',ident,{'posting_performed':False})
            return row

    def list(self,actor,company):
        with self.db.connect() as c:
            self.auth.guard(c,actor,company)
            return self.db.list(c,company,'expert_review')
