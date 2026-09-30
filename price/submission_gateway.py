"""Approved document submission with durable attempts and outcome reconciliation.

Adapters are trusted deployment configuration, never user-supplied URLs or code.
The default instance has no adapters and cannot send anything. A test adapter
cannot produce a real receipt. This module does not implement a tax portal.
"""
from __future__ import annotations
import time
from typing import Annotated,Literal,Protocol
from pydantic import Field,StrictInt,StrictStr
from .auth import Actor
from .db import DomainError,digest,now
from .upload_models import UploadInput

class Receipt(UploadInput):
    external_id:Annotated[StrictStr,Field(min_length=1,max_length=160)]
    status:Literal['received','rejected','not_found','unknown']
    document_hash:Annotated[StrictStr,Field(pattern=r'^[a-f0-9]{64}$')]
    taxpayer_reference:Annotated[StrictStr,Field(min_length=1,max_length=80)]
    tax_type:Literal['VAT','CORPORATE','WITHHOLDING','LOCAL']
    period:Annotated[StrictStr,Field(min_length=1,max_length=80)]

class TrustedAdapter(Protocol):
    name:str
    environment:str
    def submit(self,*,operation_id:str,document:bytes,metadata:dict)->dict: ...
    def reconcile(self,*,operation_id:str,metadata:dict)->dict: ...

class PackageInput(UploadInput):
    artifact_id:StrictStr
    taxpayer_reference:Annotated[StrictStr,Field(pattern=r'^[0-9]{10}$')]
    tax_type:Literal['VAT','CORPORATE','WITHHOLDING','LOCAL']
    period:Annotated[StrictStr,Field(min_length=1,max_length=80)]
    adapter_name:Annotated[StrictStr,Field(pattern=r'^[A-Za-z0-9_-]{1,80}$')]
    environment:Literal['sandbox','live']='sandbox'
    form_validation_reference:StrictStr
    idempotency_key:Annotated[StrictStr,Field(pattern=r'^[A-Za-z0-9_-]{8,120}$')]

class PackageRevision(UploadInput):
    expected_revision:Annotated[StrictInt,Field(ge=1)]
    reason:Annotated[StrictStr,Field(min_length=1,max_length=1000)]


class SubmissionGateway:
    def __init__(self,service,adapters=None,*,live_enabled=False):
        self.service,self.db,self.auth=service,service.db,service.auth
        self.adapters=dict(adapters or {})
        self.live_enabled=live_enabled is True

    def _adapter(self,package):
        adapter=self.adapters.get(package['adapter_name'])
        if adapter is None:raise DomainError('CHANNEL_NOT_CONFIGURED','실제 계약·인증된 제출 채널이 연결되지 않았습니다.')
        if adapter.environment!=package['environment']:raise DomainError('CHANNEL_ENVIRONMENT','시험 채널과 운영 채널을 혼합할 수 없습니다.')
        if package['environment']=='live' and not self.live_enabled:raise DomainError('LIVE_SUBMISSION_DISABLED','기관별 실증과 실행 동의가 확보되기 전 실제 제출은 비활성입니다.',403)
        return adapter

    def _validation(self,c,company,package):
        snapshot={key:package[key] for key in PackageInput.model_fields if key!='idempotency_key'}
        snapshot['document_hash']=package['document_hash']
        if package.get('snapshot_hash') and package['snapshot_hash']!=digest(snapshot):
            raise DomainError('PACKAGE_SNAPSHOT_CHANGED','제출 대상의 회사·세목·기간·원본이 변경되었습니다.')
        proof=self.db.get(c,company,'form_validation',package['form_validation_reference'])
        if proof.get('state')!='VALIDATED' or proof.get('document_hash')!=package['document_hash'] or proof.get('environment')!=package['environment'] or proof.get('tax_type')!=package['tax_type'] or proof.get('period')!=package['period'] or proof.get('taxpayer_reference')!=package['taxpayer_reference']:
            raise DomainError('FORM_VALIDATION_REQUIRED','제출 대상과 일치하는 법정 서식·납세자·기간 검증이 필요합니다.')
        if package['environment']=='live' and not proof.get('institution_validated'):
            raise DomainError('LIVE_FORM_NOT_VALIDATED','모의 서식검증으로 실제 신고할 수 없습니다.')
        return proof

    def prepare(self,actor,company,data:PackageInput):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','preparer'))
            artifact=c.execute('SELECT id,sha256 FROM blobs WHERE company_id=? AND id=?',(company,data.artifact_id)).fetchone()
            if not artifact:raise DomainError('NOT_FOUND','현재 회사의 제출 자료가 아닙니다.',404)
            body=data.model_dump(exclude={'idempotency_key'});body['document_hash']=artifact['sha256']
            self._validation(c,company,body)
            ident='package_'+digest({'company':company,'key':data.idempotency_key});h=digest(body)
            existing=c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='submission_package' AND id=?",(company,ident)).fetchone()
            if existing:
                old=self.db.get(c,company,'submission_package',ident)
                if old['snapshot_hash']!=h:raise DomainError('PACKAGE_KEY_CONFLICT','같은 제출 번호에 다른 내용이 있습니다.')
                return old
            row=self.db.create(c,company,'submission_package',{**body,'author':actor.id,'state':'DRAFT','reviewed_by':None,'authorized_by':None,'authorization_expires':0,'snapshot_hash':h},ident=ident)
            self.db.audit(c,company,actor.id,'submission_package.prepared',ident,{'hash':h,'environment':data.environment})
            return row

    def review(self,actor,company,ident,data:PackageRevision):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('reviewer',))
            p=self.db.get(c,company,'submission_package',ident)
            if p['revision']!=data.expected_revision or p['state']!='DRAFT':raise DomainError('PACKAGE_STATE','검토 대상이 변경되었습니다.')
            if p['author']==actor.id:raise DomainError('SELF_APPROVAL','작성자와 신고 검토자를 분리해야 합니다.',403)
            self._validation(c,company,p)
            row=self.db.replace(c,company,'submission_package',ident,{**p,'state':'REVIEWED','reviewed_by':actor.id,'review_note':data.reason},p['revision'])
            self.db.audit(c,company,actor.id,'submission_package.reviewed',ident)
            return row

    def authorize(self,actor,company,ident,data:PackageRevision):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner',))
            p=self.db.get(c,company,'submission_package',ident)
            if p['revision']!=data.expected_revision or p['state']!='REVIEWED':raise DomainError('PACKAGE_STATE','전문 검토가 끝난 신고안인지 확인해야 합니다.')
            self.auth.guard(c,Actor(p['reviewed_by'],'filing-reviewer'),company,('reviewer',))
            self._validation(c,company,p)
            row=self.db.replace(c,company,'submission_package',ident,{**p,'state':'AUTHORIZED','authorized_by':actor.id,'authorization_expires':int(time.time())+1800},p['revision'])
            self.db.audit(c,company,actor.id,'submission_package.authorized',ident,{'snapshot_hash':p['snapshot_hash'],'reason':data.reason})
            return row

    def submit(self,actor,company,ident):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner',))
            p=self.db.get(c,company,'submission_package',ident)
            if p['state']!='AUTHORIZED':raise DomainError('SUBMISSION_STATE','중복 제출 또는 결과 불명 상태에서는 재전송하지 않습니다.')
            if p['authorization_expires']<=time.time() or p['authorized_by']!=actor.id:raise DomainError('SUBMISSION_AUTHORIZATION','현재 실행 동의가 만료되었거나 실행자가 다릅니다.',403)
            self.auth.guard(c,Actor(p['reviewed_by'],'filing-reviewer'),company,('reviewer',))
            self._validation(c,company,p);adapter=self._adapter(p)
            raw=c.execute('SELECT content,sha256 FROM blobs WHERE company_id=? AND id=?',(company,p['artifact_id'])).fetchone()
            import hashlib
            if not raw or hashlib.sha256(raw['content']).hexdigest()!=p['document_hash']:raise DomainError('ARTIFACT_HASH','제출 원본 무결성이 맞지 않습니다.')
            attempt_id='send_'+digest({'package':p['id'],'snapshot':p['snapshot_hash']})
            self.db.create(c,company,'gateway_attempt',{'package_id':p['id'],'state':'SUBMITTING','environment':p['environment'],'snapshot_hash':p['snapshot_hash'],'started_at':now()},ident=attempt_id)
            p=self.db.replace(c,company,'submission_package',ident,{**p,'state':'SUBMITTING','attempt_id':attempt_id},p['revision'])
            self.db.audit(c,company,actor.id,'gateway.submit_started',attempt_id,{'package_id':ident,'environment':p['environment']})
            document=bytes(raw['content'])
        try:response=adapter.submit(operation_id=attempt_id,document=document,metadata=self._metadata(p))
        except Exception:response=None
        return self._record(company,p,attempt_id,response)

    @staticmethod
    def _metadata(package):
        return {key:package[key] for key in ('taxpayer_reference','tax_type','period','document_hash','snapshot_hash','environment')}

    def _record(self,company,package,attempt_id,response):
        try:
            receipt=Receipt.model_validate(response)
            if any(getattr(receipt,key)!=package[key] for key in ('document_hash','taxpayer_reference','tax_type','period')):raise ValueError('Receipt identity mismatch')
            status=receipt.status;body=receipt.model_dump()
        except Exception:status='unknown';body=None
        translated={'received':'SIM_RECEIVED' if package['environment']=='sandbox' else 'RECEIVED','rejected':'REJECTED','unknown':'OUTCOME_UNKNOWN','not_found':'OUTCOME_UNKNOWN'}[status]
        with self.db.transaction() as c:
            current=self.db.get(c,company,'submission_package',package['id'])
            attempt=self.db.get(c,company,'gateway_attempt',attempt_id)
            if current['state'] not in ('SUBMITTING','OUTCOME_UNKNOWN'):return current
            row=self.db.replace(c,company,'submission_package',current['id'],{**current,'state':translated,'receipt':body,'paid':False},current['revision'])
            self.db.replace(c,company,'gateway_attempt',attempt_id,{**attempt,'state':translated,'receipt':body},attempt['revision'])
            self.db.audit(c,company,'system:submission-gateway','gateway.outcome_recorded',attempt_id,{'state':translated,'environment':package['environment']})
            return row

    def reconcile(self,actor,company,ident):
        with self.db.connect() as c:
            self.auth.guard(c,actor,company,('owner','reviewer'))
            p=self.db.get(c,company,'submission_package',ident)
            if p['state'] not in ('SUBMITTING','OUTCOME_UNKNOWN'):return p
            adapter=self._adapter(p)
        try:response=adapter.reconcile(operation_id=p['attempt_id'],metadata=self._metadata(p))
        except Exception:response=None
        return self._record(company,p,p['attempt_id'],response)

    def list(self,actor,company):
        with self.db.connect() as c:
            self.auth.guard(c,actor,company)
            return {'packages':self.db.list(c,company,'submission_package'),'configured_adapters':list(self.adapters),'live_enabled':self.live_enabled}
