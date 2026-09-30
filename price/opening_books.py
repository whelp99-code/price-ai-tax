"""One-time opening balances from parsed original documents and independent review.

Opening VAT balances are carry-in assets/liabilities, never current-period VAT.
Original statements and prior journals are not overwritten by this import.
"""
from __future__ import annotations
import json
from datetime import date
from decimal import Decimal
from typing import Annotated
from pydantic import Field,StrictInt,StrictStr
from .upload_models import UploadInput
from .auth import Actor
from .db import DomainError,digest,now
from .models import Line
from .calculations import validate_lines
from .accounts import ACCOUNT_INFO


class OpeningRequest(UploadInput):
    document_id:Annotated[StrictStr,Field(min_length=1,max_length=160)]
    idempotency_key:Annotated[StrictStr,Field(pattern=r'^[A-Za-z0-9_-]{8,120}$')]


class OpeningReview(UploadInput):
    expected_revision:Annotated[StrictInt,Field(ge=1)]
    reason:Annotated[StrictStr,Field(min_length=1,max_length=2000)]


def _hash(plan):
    return digest({k:plan[k] for k in ('document_id','source_hash','date','lines','evidence_id','author')})


class OpeningBooks:
    def __init__(self,service,*,initialize=True):
        self.service,self.db,self.auth=service,service.db,service.auth
        if initialize:
            with self.db.connect() as c:
                c.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_opening_journal_per_company ON objects(company_id) WHERE kind='journal' AND json_extract(body,'$.state')='POSTED' AND json_extract(body,'$.opening_batch_id') IS NOT NULL")

    def _existing(self,c,company):
        return next((j for j in self.db.list(c,company,'journal') if j.get('opening_batch_id') and j['state']=='POSTED'),None)

    def _document(self,c,company,ident):
        row=c.execute('SELECT * FROM upload_documents WHERE company_id=? AND id=?',(company,ident)).fetchone()
        if not row or row['state']!='NORMALIZED':raise DomainError('OPENING_SOURCE_CHANGED','기초 자료가 변경되었거나 읽기 검증을 마치지 못했습니다.')
        summary=json.loads(row['result']) if row['result'] else {}
        if summary.get('document_kind') not in ('opening_balances','opening_balance'):raise DomainError('OPENING_SOURCE_KIND','확인된 기초잔액 원본이 필요합니다.')
        records=[self.db.unpack(r) for r in c.execute("SELECT * FROM objects WHERE company_id=? AND kind='source_record' AND json_extract(body,'$.document_id')=? AND COALESCE(json_extract(body,'$.lifecycle'),'CURRENT')='CURRENT' ORDER BY id",(company,ident))]
        if not records or any(r['kind']!='opening_balance' or r.get('validation')!='NORMALIZED' for r in records):raise DomainError('OPENING_SOURCE_KIND','기초잔액 항목이 정상적으로 읽히지 않았습니다.')
        dates={r['values']['date'] for r in records};currencies={r['values']['currency'] for r in records}
        if len(dates)!=1 or None in dates or currencies!={'KRW'}:raise DomainError('OPENING_PERIOD_CURRENCY','하나의 기초일과 원화 기준이 필요합니다.')
        balances={}
        for record in records:
            value=record['values'];account=value['ledger_account']
            if account not in ACCOUNT_INFO or ACCOUNT_INFO[account][1] in ('income','expense'):raise DomainError('OPENING_ACCOUNT_REVIEW','기초 계정 범위를 검토해야 합니다.')
            debit,credit=Decimal(value['debit']),Decimal(value['credit'])
            if any(v<0 or v!=v.to_integral_value() for v in (debit,credit)):raise DomainError('OPENING_AMOUNT_INVALID','원본 잔액을 확인해야 합니다.')
            balances[account]=balances.get(account,0)+int(debit-credit)
        lines=[Line(account=k,side='debit' if v>0 else 'credit',amount=abs(v)).model_dump() for k,v in sorted(balances.items()) if v]
        validate_lines(lines)
        source_hash=digest({'document':dict(row),'sources':[(r['id'],r['revision'],r['values']) for r in records]})
        return next(iter(dates)),lines,row['blob_id'],source_hash

    def propose(self,actor,company,data:OpeningRequest):
        key='opening_'+digest({'company':company,'key':data.idempotency_key})
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','preparer'))
            if c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='opening_batch' AND id=?",(company,key)).fetchone():
                old=self.db.get(c,company,'opening_batch',key)
                if old['document_id']!=data.document_id:raise DomainError('OPENING_KEY_CONFLICT','같은 요청에 다른 원본이 있습니다.')
                return old
            if self._existing(c,company):raise DomainError('OPENING_ALREADY_POSTED','기초잔액이 이미 장부에 반영되었습니다. 정정 절차를 사용하세요.')
            when,lines,evidence,source_hash=self._document(c,company,data.document_id)
            value={'document_id':data.document_id,'source_hash':source_hash,'date':when,'lines':lines,'evidence_id':evidence,
                   'author':actor.id,'state':'DRAFT','reviewed_by':None,'review_hash':None,'journal_id':None,
                   'source_kind':'parsed_opening_balances','current_tax_effect':False}
            result=self.db.create(c,company,'opening_batch',value,ident=key)
            self.db.audit(c,company,actor.id,'opening.proposed',key,{'document':data.document_id,'source_hash':source_hash})
            return result

    def approve(self,actor,company,ident,data:OpeningReview):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','reviewer'))
            plan=self.db.get(c,company,'opening_batch',ident)
            if actor.id==plan['author']:raise DomainError('SELF_APPROVAL','기초자료 작성자와 다른 검토자가 확인해야 합니다.',403)
            self.auth.guard(c,actor,company,('reviewer',))
            if not data.reason.strip():raise DomainError('REASON_REQUIRED','전기말 장부 대조 결과를 기록해야 합니다.',422)
            if plan['revision']!=data.expected_revision or plan['state']!='DRAFT':raise DomainError('STALE_VERSION','기초자료 검토 상태를 다시 확인하세요.')
            if self._document(c,company,plan['document_id'])[3]!=plan['source_hash']:raise DomainError('OPENING_SOURCE_CHANGED','기초자료가 변경되었습니다.')
            result=self.db.replace(c,company,'opening_batch',ident,{**plan,'state':'APPROVED','reviewed_by':actor.id,'review_hash':_hash(plan),'review_note':data.reason},plan['revision'])
            self.db.audit(c,company,actor.id,'opening.reviewed',ident,{'hash':result['review_hash']})
            return result

    def _validated(self,c,company,plan):
        if plan['state']!='APPROVED' or plan['reviewed_by']==plan['author'] or plan['review_hash']!=_hash(plan):raise DomainError('OPENING_NOT_APPROVED','독립 검토한 기초잔액만 반영합니다.')
        self.auth.guard(c,Actor(plan['reviewed_by'],'opening-reviewer'),company,('reviewer',))
        when,lines,evidence,source_hash=self._document(c,company,plan['document_id'])
        if source_hash!=plan['source_hash'] or when!=plan['date'] or lines!=plan['lines']:raise DomainError('OPENING_SOURCE_CHANGED','기초자료가 변경되었습니다.')
        self.service._evidence_check(c,company,[evidence],required=True)
        return True

    def post(self,actor,company,ident):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','preparer','reviewer'))
            plan=self.db.get(c,company,'opening_batch',ident)
            if plan.get('journal_id'):return self.db.get(c,company,'journal',plan['journal_id'])
            self._validated(c,company,plan)
            if plan['date']>date.today().isoformat():raise DomainError('OPENING_FUTURE','미래 기초잔액을 확정하지 않습니다.')
            if self._existing(c,company):raise DomainError('OPENING_ALREADY_POSTED','기초잔액이 이미 반영되었습니다.')
            if any(j['state']=='POSTED' and j['event_date']<plan['date'] for j in self.db.list(c,company,'journal')):
                raise DomainError('OPENING_DATE_CONFLICT','기초일 이전 확정 장부가 있어 이관 범위를 다시 검토해야 합니다.')
            data={'event_date':plan['date'],'description':'검토된 전기말 잔액 이관','lines':plan['lines'],'evidence_ids':[plan['evidence_id']]}
            journal=self.service._new_journal(c,Actor('system:opening-ledger','opening-ledger'),company,data)
            journal.update(opening_batch_id=ident,current_period_activity=False)
            self.service._journal_valid(c,company,journal)
            journal.update(state='POSTED',approved_by=plan['reviewed_by'],approval_hash=self.service.journal_hash(journal),authorization_kind='INDEPENDENT_OPENING_IMPORT')
            result=self.db.replace(c,company,'journal',journal['id'],journal,journal['revision'])
            self.db.replace(c,company,'opening_batch',ident,{**plan,'state':'POSTED','journal_id':journal['id'],'posted_at':now()},plan['revision'])
            self.db.audit(c,company,actor.id,'opening.posted',journal['id'],{'batch_id':ident,'source_hash':plan['source_hash'],'current_tax_effect':False})
            return result

    def list(self,actor,company):
        with self.db.connect() as c:
            self.auth.guard(c,actor,company)
            return self.db.list(c,company,'opening_batch')

    def run_once(self,company=None):
        with self.db.connect() as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='upload_documents'").fetchone():return {'processed':False}
            query="SELECT d.company_id,d.id,d.revision,j.requested_by FROM upload_documents d JOIN upload_jobs j ON j.company_id=d.company_id AND j.document_id=d.id WHERE d.state='NORMALIZED' AND json_extract(d.result,'$.document_kind') IN ('opening_balances','opening_balance')"
            if company:query+=' AND d.company_id=?'
            candidates=[dict(r) for r in c.execute(query,[company] if company else [])]
        for item in candidates:
            key='auto-opening-'+digest({'document':item['id'],'revision':item['revision']})
            ident='opening_'+digest({'company':item['company_id'],'key':key})
            with self.db.connect() as c:
                if c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='opening_batch' AND id=?",(item['company_id'],ident)).fetchone():continue
                if self._existing(c,item['company_id']):continue
                if any(p['document_id']==item['id'] and p['state'] in ('DRAFT','APPROVED') for p in self.db.list(c,item['company_id'],'opening_batch')):continue
            try:
                result=self.propose(Actor(item['requested_by'],'source-uploader'),item['company_id'],OpeningRequest(document_id=item['id'],idempotency_key=key))
                return {'processed':True,'state':'PROPOSED','id':result['id']}
            except DomainError:
                continue
        return {'processed':False}


def validate_opening_entry(service,c,company,journal):
    plan=service.db.get(c,company,'opening_batch',journal['opening_batch_id'])
    OpeningBooks(service,initialize=False)._validated(c,company,plan)
    if journal['lines']!=plan['lines'] or journal['event_date']!=plan['date'] or journal['evidence_ids']!=[plan['evidence_id']] or journal.get('transaction_id'):
        raise DomainError('OPENING_JOURNAL_MISMATCH','검토된 기초 잔액과 장부 반영 내용이 다릅니다.')
