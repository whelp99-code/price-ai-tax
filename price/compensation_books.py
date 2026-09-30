"""Source-backed withholding payment ledger for independently reviewed recipients.

Uses the actual bank source for the net amount already paid, with the declared
contract/work record for gross income. It never initiates payroll transfers.
Supports the two rule profiles in withholding_rules; general monthly salaries,
retirement, foreign recipients and insurance remain separate rule modules.
"""
from __future__ import annotations
from datetime import date
from typing import Annotated
from pydantic import Field,StrictStr,StrictInt,StrictBool
from .upload_models import UploadInput
from .withholding_rules import WithholdingPayment,calculate_withholding
from .casework import Casework
from .event_settlements import exact_krw
from .db import DomainError,digest,now
from .auth import Actor

Text=Annotated[StrictStr,Field(min_length=1,max_length=160)]

class CompensationInput(UploadInput):
    recipient_reference:Text
    bank_event_id:Text
    payment:WithholdingPayment
    evidence_ids:Annotated[list[StrictStr],Field(min_length=1,max_length=30)]
    reference:Annotated[StrictStr,Field(pattern=r'^[A-Za-z0-9가-힣_.:-]{3,120}$')]

class CompensationReview(UploadInput):
    expected_revision:Annotated[StrictInt,Field(ge=1)]
    reason:Annotated[StrictStr,Field(min_length=1,max_length=3000)]
    authority_ids:Annotated[list[StrictStr],Field(min_length=1,max_length=20)]
    prior_expense_absent_verified:StrictBool
    recognition_period_verified:StrictBool
    recipient_matches_bank_verified:StrictBool


def _digest(plan):return digest({k:plan.get(k) for k in ('input','calculation','bank_context_hash','lines','author','authorities','accounting_checks')})


class CompensationBooks:
    def __init__(self,service,*,today=None):
        self.service,self.db,self.auth=service,service.db,service.auth
        self.events=Casework(service);self.today=today or date.today

    def _bank(self,c,company,ident,calculation):
        bank=self.events._fresh_event(c,company,ident)
        if bank['issues'] or bank['kind']!='bank' or bank['direction']!='payment':raise DomainError('PAYROLL_BANK_SOURCE','확인된 회사 통장 출금 원본이 필요합니다.')
        if bank['values']['date'][:10]!=calculation['paid_on'] or exact_krw(bank)!=calculation['net_to_recipient']:
            raise DomainError('PAYROLL_NET_MISMATCH','실제 출금 날짜·금액이 원천징수 후 지급액과 다릅니다.')
        if bank['values']['date'][:10]>self.today().isoformat():raise DomainError('PAYROLL_FUTURE','미래 지급을 장부에 확정하지 않습니다.')
        if any(j['state']=='POSTED' and j.get('economic_event_id')==ident for j in self.db.list(c,company,'journal')):
            raise DomainError('PAYROLL_PAYMENT_DUPLICATE','같은 통장 거래가 이미 장부에 반영되었습니다.')
        if any(link['payment_id']==ident for link in self.db.list(c,company,'event_settlement')):
            raise DomainError('PAYROLL_ALREADY_ALLOCATED','이 출금은 청구서 결제로 연결되어 있습니다. 중복 원가를 확인하세요.')
        return bank

    def _authorities(self,c,company,ids,when):
        refs=[]
        for ident in ids:
            rule=self.db.get(c,company,'authority',ident)
            if not rule.get('reviewed') or not rule.get('reviewed_by') or not rule['effective_from']<=when<=rule['effective_to']:
                raise DomainError('PAYROLL_AUTHORITY','지급 시점에 적용되는 검토된 근거가 필요합니다.')
            self.auth.guard(c,Actor(rule['reviewed_by'],'payroll-authority-reviewer'),company,('owner','reviewer'))
            refs.append({'id':rule['id'],'revision':rule['revision'],'hash':digest(rule)})
        return refs

    def propose(self,actor,company,data:CompensationInput):
        data=CompensationInput.model_validate(data)
        values=data.model_dump(mode='json')
        ident='compensation_'+digest({'company':company,'reference':data.reference})
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','preparer'))
            if c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='compensation' AND id=?",(company,ident)).fetchone():
                previous=self.db.get(c,company,'compensation',ident)
                if previous['input']!=values:raise DomainError('PAYROLL_REFERENCE_CONFLICT','같은 지급명세 번호에 다른 내용이 있습니다.')
                return previous
            calculation=calculate_withholding(data.payment)
            bank=self._bank(c,company,data.bank_event_id,calculation)
            self.service._evidence_check(c,company,data.evidence_ids,required=True)
            debit='payroll_expense' if data.payment.category=='resident_daily_wage' else 'expense'
            lines=[{'account':debit,'side':'debit','amount':calculation['gross']},{'account':'cash','side':'credit','amount':calculation['net_to_recipient']}]
            if calculation['income_tax']:lines.append({'account':'withholding_payable','side':'credit','amount':calculation['income_tax']})
            if calculation['local_income_tax']:lines.append({'account':'local_withholding_payable','side':'credit','amount':calculation['local_income_tax']})
            if not calculation['gross'] or not calculation['net_to_recipient']:raise DomainError('PAYROLL_ZERO_PAYMENT','실제 지급액이 없는 자료는 별도 정산 검토가 필요합니다.')
            p={'input':values,'calculation':calculation,'bank_context_hash':bank['context_hash'],'lines':lines,'author':actor.id,
               'state':'DRAFT','reviewed_by':None,'review_hash':None,'journal_id':None,'authorities':[],'bank_transfer_initiated':False,
               'expense_recognition':'cash_payment_basis_requires_period_review','official_statement_submitted':False}
            result=self.db.create(c,company,'compensation',p,ident=ident)
            self.db.audit(c,company,actor.id,'compensation.proposed',ident,{'category':data.payment.category,'source_event':bank['id'],'paid_on':calculation['paid_on']})
            return result

    def review(self,actor,company,ident,data:CompensationReview):
        if not all((data.prior_expense_absent_verified,data.recognition_period_verified,data.recipient_matches_bank_verified)):
            raise DomainError('PAYROLL_ACCOUNTING_REVIEW','이미 기록된 비용·귀속기간·실제 수취인 대조를 완료해야 합니다.')
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','reviewer'))
            p=self.db.get(c,company,'compensation',ident)
            if actor.id==p['author']:raise DomainError('SELF_APPROVAL','실제 계약·소득구분·비과세·귀속기간은 다른 검토자가 확인해야 합니다.',403)
            self.auth.guard(c,actor,company,('reviewer',))
            if p['state']!='DRAFT' or p['revision']!=data.expected_revision:raise DomainError('STALE_VERSION','지급 자료가 변경되었습니다.')
            if not data.reason.strip():raise DomainError('REASON_REQUIRED','소득구분·비과세·귀속기간 판단 근거가 필요합니다.',422)
            bank=self._bank(c,company,p['input']['bank_event_id'],p['calculation'])
            if bank['context_hash']!=p['bank_context_hash']:raise DomainError('PAYROLL_SOURCE_CHANGED','원본 출금이 변경되었습니다.')
            refs=self._authorities(c,company,data.authority_ids,p['calculation']['paid_on'])
            reviewed={**p,'state':'REVIEWED','reviewed_by':actor.id,'authorities':refs,'review_note':data.reason,'accounting_checks':data.model_dump(exclude={'expected_revision','reason','authority_ids'})}
            reviewed['review_hash']=_digest(reviewed)
            result=self.db.replace(c,company,'compensation',ident,reviewed,p['revision'])
            self.db.audit(c,company,actor.id,'compensation.reviewed',ident,{'hash':result['review_hash'],'professional_license_verified':False})
            return result

    def post(self,actor,company,ident):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','preparer','reviewer'))
            p=self.db.get(c,company,'compensation',ident)
            if p['state']=='POSTED':return self.db.get(c,company,'journal',p['journal_id'])
            if p['state']!='REVIEWED' or p['author']==p['reviewed_by'] or _digest(p)!=p['review_hash']:
                raise DomainError('PAYROLL_NOT_REVIEWED','독립 검토된 지급 자료만 장부에 반영합니다.')
            if not all(p.get('accounting_checks',{}).get(k) is True for k in ('prior_expense_absent_verified','recognition_period_verified','recipient_matches_bank_verified')):
                raise DomainError('PAYROLL_ACCOUNTING_REVIEW','검토한 지급 회계 조건이 없습니다.')
            self.auth.guard(c,Actor(p['reviewed_by'],'payroll-reviewer'),company,('reviewer',))
            if calculate_withholding(WithholdingPayment(**p['input']['payment']))!=p['calculation']:raise DomainError('PAYROLL_RULE_CHANGED','원천징수 규칙이나 계산 결과가 변경되었습니다.')
            if self._authorities(c,company,[x['id'] for x in p['authorities']],p['calculation']['paid_on'])!=p['authorities']:
                raise DomainError('PAYROLL_AUTHORITY_CHANGED','검토에 사용한 근거가 변경되었습니다.')
            bank=self._bank(c,company,p['input']['bank_event_id'],p['calculation'])
            if bank['context_hash']!=p['bank_context_hash']:raise DomainError('PAYROLL_SOURCE_CHANGED','원본 출금이 변경되었습니다.')
            evidence=sorted(set(bank['evidence_ids']+p['input']['evidence_ids']))
            data={'event_date':p['calculation']['paid_on'],'description':'검토한 보수 지급과 원천징수','lines':p['lines'],'evidence_ids':evidence}
            j=self.service._new_journal(c,Actor('system:compensation','compensation'),company,data)
            self.service._journal_valid(c,company,j)
            j.update(state='POSTED',approved_by=p['reviewed_by'],approval_hash=self.service.journal_hash(j),compensation_id=ident,
                     economic_event_id=bank['id'],authorization_kind='REVIEWED_COMPENSATION_PAYMENT',withholding_breakdown={'national':p['calculation']['income_tax'],'local':p['calculation']['local_income_tax']})
            posted=self.db.replace(c,company,'journal',j['id'],j,j['revision'])
            self.db.replace(c,company,'compensation',ident,{**p,'state':'POSTED','journal_id':j['id'],'posted_at':now()},p['revision'])
            self.db.audit(c,company,actor.id,'compensation.booked',j['id'],{'plan':ident,'bank_transfer_initiated':False})
            return posted

    def list(self,actor,company):
        with self.db.connect() as c:
            self.auth.guard(c,actor,company)
            return self.db.list(c,company,'compensation')

    def monthly_statement(self,actor,company,year,month):
        if type(year) is not int or type(month) is not int or not 2000<=year<=2100 or not 1<=month<=12:raise DomainError('PERIOD','원천세 조회 기간이 잘못되었습니다.',422)
        prefix=f'{year:04d}-{month:02d}';rows=[];blockers=[]
        with self.db.connect() as c:
            self.auth.guard(c,actor,company)
            for r in self.db.list(c,company,'compensation'):
                if not r['calculation']['paid_on'].startswith(prefix):continue
                if r['state']!='POSTED':blockers.append({'id':r['id'],'code':'COMPENSATION_NOT_POSTED'});continue
                j=self.db.get(c,company,'journal',r['journal_id'])
                if any(x.get('reversal_of')==j['id'] and x['state']=='POSTED' for x in self.db.list(c,company,'journal')):
                    blockers.append({'id':r['id'],'code':'COMPENSATION_REVERSED'});continue
                rows.append({'id':r['id'],'recipient_reference':r['input']['recipient_reference'],'paid_on':r['calculation']['paid_on'],
                             'category':r['calculation']['category'],'gross':r['calculation']['gross'],'national':r['calculation']['income_tax'],
                             'local':r['calculation']['local_income_tax'],'journal_id':j['id'],'evidence_ids':j['evidence_ids']})
            return {'period':prefix,'rows':rows,'gross':sum(x['gross'] for x in rows),'national':sum(x['national'] for x in rows),'local':sum(x['local'] for x in rows),
                    'blockers':blockers,'official_filing_format':False,'tax_paid':False,'notice':'확정 장부와 지급 근거가 연결된 원천징수 검토 목록입니다. 실제 신고서 제출·납부는 별도입니다.'}
