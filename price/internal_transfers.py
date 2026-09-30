"""Book two source-confirmed movements between the company's own accounts.

There is no bank-transfer operation in this module. It recognizes money that
already moved. Cash-management movements are separated from external receipts
and payments; this is internal bookkeeping, not a statutory statement claim.
"""
from __future__ import annotations
from datetime import date
from decimal import Decimal
from typing import Annotated
from pydantic import Field, StrictInt, StrictStr
from .db import DomainError, digest, now
from .auth import Actor
from .upload_models import UploadInput
from .casework import Casework


class TransferReview(UploadInput):
    expected_revision: Annotated[StrictInt, Field(ge=1)]
    reason: Annotated[StrictStr, Field(min_length=1, max_length=2000)]


def source_contributors(db,c,company,events):
    """Origin stays human even if the proposal was created by a worker."""
    people=set()
    profile=db.list(c,company,'company_facts')
    if profile and profile[0].get('confirmed_by'):people.add(profile[0]['confirmed_by'])
    for event in events:
        for document in event['document_ids']:
            people.update(r[0] for r in c.execute('SELECT DISTINCT b.submitted_by FROM upload_batches b JOIN upload_files f ON f.company_id=b.company_id AND f.batch_id=b.id WHERE b.company_id=? AND f.document_id=?',(company,document)))
        people.update(db.unpack(r)['answered_by'] for r in c.execute("SELECT * FROM objects WHERE company_id=? AND kind='event_fact' AND json_extract(body,'$.event_id')=? AND json_extract(body,'$.active')=1",(company,event['id'])))
    return people


class InternalTransfers:
    def __init__(self,service,*,today=None):
        self.service,self.db,self.auth=service,service.db,service.auth
        self.work=Casework(service);self.today=today or date.today

    def _pairs(self,c,company):
        banks=[];context=self.work._source_context(c,company)
        for raw in self.db.list(c,company,'economic_event'):
            if raw.get('lifecycle')!='CURRENT' or raw['kind']!='bank':continue
            try:event=self.work._fresh_event(c,company,raw['id'],context=context)
            except DomainError:continue
            value=event['values'];account=value.get('account');counter=value.get('counterparty_account')
            if event['issues'] or event['ownership']!='owner_confirmed' or value['currency']!='KRW' or not counter or account==counter:continue
            if any(ch in counter for ch in ('X','*')):continue
            amount=Decimal(value['gross'])
            if amount!=amount.to_integral_value() or not 0<amount<=1_000_000_000_000:continue
            facts=self.work._answers(c,company,event)
            if facts.get('cash_reason',{}).get('answer') not in (None,'internal_transfer'):continue
            banks.append((event,int(amount),facts))
        outs=[r for r in banks if r[0]['direction']=='payment'];ins=[r for r in banks if r[0]['direction']=='receipt']
        outgoing={};incoming={};by_key={}
        for inc,amount2,facts2 in ins:
            b=inc['values'];by_key.setdefault((b['account'],b['counterparty_account'],amount2,b['date'][:10]),[]).append(inc)
        for out,amount,facts in outs:
            a=out['values']
            for inc in by_key.get((a['counterparty_account'],a['account'],amount,a['date'][:10]),[]):
                outgoing.setdefault(out['id'],[]).append(inc['id']);incoming.setdefault(inc['id'],[]).append(out['id'])
        all_events={e['id']:(e,amount,facts) for e,amount,facts in banks};result=[]
        for out_id,candidates in sorted(outgoing.items()):
            if len(candidates)!=1 or incoming[candidates[0]]!=[out_id]:continue
            out,amount,facts=all_events[out_id];inc,_,facts2=all_events[candidates[0]]
            ids=[out['id'],inc['id']]
            result.append({'event_ids':ids,'event_hashes':[out['context_hash'],inc['context_hash']],
                'fact_hashes':[digest(facts),digest(facts2)],'event_date':out['values']['date'][:10],
                'from_account':out['values']['account'],'to_account':inc['values']['account'],
                'amount':amount,'currency':'KRW','evidence_ids':sorted(set(out['evidence_ids']+inc['evidence_ids'])),
                'document_ids':sorted(set(out['document_ids']+inc['document_ids'])),
                'contributor_ids':sorted(source_contributors(self.db,c,company,[out,inc])),
                'matching':'unique_reciprocal_owned_accounts_same_day_amount/1'})
        return result

    def _refresh(self,c,company,actor):
        pairs=self._pairs(c,company);created=[]
        for basis in pairs:
            ident='transfer_'+digest({'company':company,'events':basis['event_ids'],'basis':basis})
            if c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='internal_transfer' AND id=?",(company,ident)).fetchone():continue
            if any(j['state']=='POSTED' and j.get('economic_event_id') in basis['event_ids'] for j in self.db.list(c,company,'journal')):continue
            record=self.db.create(c,company,'internal_transfer',{'basis':basis,'state':'DRAFT','author':actor,'reviewed_by':None,'review_hash':None,'journal_ids':[]},ident=ident)
            self.db.audit(c,company,actor,'transfer.proposed',ident,{'source_events':basis['event_ids'],'bank_transfer_initiated':False})
            created.append(record['id'])
        return created

    def refresh(self,actor,company):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','preparer','reviewer'))
            return {'created':self._refresh(c,company,actor.id)}

    def run_once(self,company=None):
        with self.db.transaction() as c:
            companies=[r[0] for r in c.execute('SELECT id FROM companies'+(' WHERE id=?' if company else ''),[company] if company else [])]
            for ident in companies:
                created=self._refresh(c,ident,'system:transfer-proposals')
                if created:return {'processed':True,'state':'PROPOSED','plans':len(created)}
        return {'processed':False}

    def _validate(self,c,company,record):
        basis=record['basis']
        if basis not in self._pairs(c,company):raise DomainError('TRANSFER_SOURCE_CHANGED','계좌·원본·금액·사실 또는 유일한 연결 조건이 바뀌었습니다.')
        if basis['event_date']>self.today().isoformat():raise DomainError('TRANSFER_FUTURE','미래 날짜의 이체를 장부에 미리 반영하지 않습니다.')
        self.service._locked(c,company,basis['event_date'])
        for j in self.db.list(c,company,'journal'):
            if j['state']=='POSTED' and j.get('economic_event_id') in basis['event_ids']:
                raise DomainError('TRANSFER_ALREADY_POSTED','연결한 은행 거래 중 이미 반영된 거래가 있습니다.')
        self.service._evidence_check(c,company,basis['evidence_ids'],required=True)
        return basis

    def approve(self,actor,company,ident,data:TransferReview):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('reviewer',))
            p=self.db.get(c,company,'internal_transfer',ident)
            if p['state']!='DRAFT' or p['revision']!=data.expected_revision:raise DomainError('STALE_VERSION','이체 검토 대상이 바뀌었습니다.')
            basis=self._validate(c,company,p)
            if actor.id in set(basis['contributor_ids'])|{p['author']}:raise DomainError('SELF_APPROVAL','원본·회사 사실·처리안 작성자와 다른 담당자가 검토해야 합니다.',403)
            if not data.reason.strip():raise DomainError('REASON_REQUIRED','대조 근거가 필요합니다.',422)
            p=self.db.replace(c,company,'internal_transfer',ident,{**p,'state':'APPROVED','reviewed_by':actor.id,'review_hash':digest(basis),'review_note':data.reason},p['revision'])
            self.db.audit(c,company,actor.id,'transfer.approved',ident,{'hash':p['review_hash']})
            return self._view(p)

    def post(self,actor,company,ident):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','preparer','reviewer'))
            p=self.db.get(c,company,'internal_transfer',ident)
            if p['state']=='POSTED':return self._view(p)
            if p['state']!='APPROVED' or p['review_hash']!=digest(p['basis']):raise DomainError('TRANSFER_NOT_APPROVED','독립 검토한 이체 대사만 장부에 반영합니다.')
            self.auth.guard(c,Actor(p['reviewed_by'],'transfer-reviewer'),company,('reviewer',))
            basis=self._validate(c,company,p)
            if p['reviewed_by'] in set(basis['contributor_ids'])|{p['author']}:raise DomainError('SELF_APPROVAL','독립 검토 조건이 충족되지 않습니다.',403)
            amount=basis['amount'];ids=[]
            for index,(debit,credit,account,direction) in enumerate((('transfer_clearing','cash',basis['from_account'],'payment'),('cash','transfer_clearing',basis['to_account'],'receipt'))):
                j=self.service._new_journal(c,Actor('system:transfer-books','transfer-books'),company,{'event_date':basis['event_date'],'description':'확인된 회사 계좌간 자금 이동','lines':[{'account':debit,'side':'debit','amount':amount},{'account':credit,'side':'credit','amount':amount}],'evidence_ids':basis['evidence_ids']})
                self.service._journal_valid(c,company,j)
                j.update(state='POSTED',approved_by=p['reviewed_by'],approval_hash=self.service.journal_hash(j),economic_event_id=basis['event_ids'][index],internal_transfer_id=ident,
                         bank_instrument=account,bank_direction=direction,authorization_kind='REVIEWED_INTERNAL_TRANSFER',transfer_basis_hash=p['review_hash'],external_cash_flow=False)
                j=self.db.replace(c,company,'journal',j['id'],j,j['revision']);ids.append(j['id'])
                self.db.audit(c,company,actor.id,'transfer.leg_posted',j['id'],{'plan':ident,'bank_transfer_initiated':False})
            p=self.db.replace(c,company,'internal_transfer',ident,{**p,'state':'POSTED','journal_ids':ids,'posted_at':now()},p['revision'])
            return self._view(p)

    @staticmethod
    def _view(p):
        b=p['basis']
        return {'id':p['id'],'revision':p['revision'],'state':p['state'],'amount':b['amount'],'date':b['event_date'],
                'event_ids':b['event_ids'],'document_ids':b['document_ids'],'journal_ids':p['journal_ids'],
                'from_account':b['from_account'],'to_account':b['to_account'],'reviewed_by':p['reviewed_by'],
                'bank_transfer_initiated':False,'income_or_expense_created':False,'accounting_complete':p['state']=='POSTED'}

    def list(self,actor,company):
        with self.db.connect() as c:
            self.auth.guard(c,actor,company);c.execute('BEGIN')
            pairs=self._pairs(c,company);items=[]
            reversed_ids={j.get('reversal_of') for j in self.db.list(c,company,'journal') if j['state']=='POSTED'}
            for p in self.db.list(c,company,'internal_transfer'):
                view=self._view(p)
                if p['basis'] not in pairs and p['state']!='POSTED':view['state']='SOURCE_CHANGED';view['accounting_complete']=False
                if p['state']=='POSTED' and any(i in reversed_ids for i in p['journal_ids']):view['state']='REVERSAL_REVIEW';view['accounting_complete']=False
                items.append(view)
            return {'items':items,'notice':'이미 이동한 회사 자금을 대사합니다. 은행 송금은 실행하지 않습니다.'}
