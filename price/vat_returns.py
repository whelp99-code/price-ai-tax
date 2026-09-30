"""Line-item VAT workpapers generated from posted sources and reviewed adjustments.

Follows the NTS general-taxpayer calculation flow (retrieved 2026-09-29):
https://www.nts.go.kr/nts/cm/cntnts/cntntsView.do?cntntsId=7695&mi=2272
Each result retains original transaction/journal references and a versioned
basis. It is not certified statutory e-filing bytes and never submits itself.
"""
from __future__ import annotations
from collections import defaultdict
from typing import Annotated,Literal
from pydantic import StrictInt,StrictStr,Field
from .upload_models import UploadInput
from .db import DomainError,digest,now
from .auth import Actor
from .vat_rules_kr import FLOW

AdjustmentKind=Literal['output_omitted','bad_debt_output_adjustment','input_omitted','deferred_import_input',
 'other_deductible_input','nondeductible_input','reviewed_tax_credit','prior_unrefunded',
 'pre_notified_tax','acquisition_substitute_paid','purchaser_special_paid','card_company_paid','penalty',
 'zero_other_supply','exempt_supply_adjustment','corporate_card_issue_credit']

class VatAdjustmentInput(UploadInput):
    period_id:StrictStr
    kind:AdjustmentKind
    supply_amount:Annotated[StrictInt,Field(ge=-1_000_000_000_000,le=1_000_000_000_000)]=0
    tax_amount:Annotated[StrictInt,Field(ge=-1_000_000_000_000,le=1_000_000_000_000)]
    evidence_ids:Annotated[list[StrictStr],Field(min_length=1,max_length=30)]
    authority_ids:Annotated[list[StrictStr],Field(min_length=1,max_length=20)]
    reason:Annotated[StrictStr,Field(min_length=1,max_length=2000)]
    reference:Annotated[StrictStr,Field(min_length=3,max_length=160)]

class VatReview(UploadInput):
    expected_revision:Annotated[StrictInt,Field(ge=1)]
    reason:Annotated[StrictStr,Field(min_length=1,max_length=2000)]

class VatBuild(UploadInput):
    period_id:StrictStr
    taxpayer_kind:Literal['corporate_general']='corporate_general'


def _adj_hash(record):return digest({k:record[k] for k in ('input','author','authority_refs')})


class VatReturns:
    def __init__(self,service):self.service,self.db,self.auth=service,service.db,service.auth

    def _authorities(self,c,company,ids,period):
        refs=[]
        for ident in ids:
            a=self.db.get(c,company,'authority',ident)
            if not a.get('reviewed') or not a.get('reviewed_by') or a['effective_from']>period['start'] or a['effective_to']<period['end']:
                raise DomainError('VAT_AUTHORITY_NOT_APPLICABLE','해당 과세기간 전체에 적용되는 검토 근거가 필요합니다.')
            self.auth.guard(c,Actor(a['reviewed_by'],'vat-rule-reviewer'),company,('owner','reviewer'))
            refs.append({'id':a['id'],'revision':a['revision'],'hash':digest(a)})
        return refs

    def add_adjustment(self,actor,company,data:VatAdjustmentInput):
        value=VatAdjustmentInput.model_validate(data).model_dump(mode='json')
        if data.kind=='corporate_card_issue_credit':raise DomainError('CORPORATE_CREDIT_NOT_ALLOWED','법인에게 개인사업자용 카드 발행공제를 적용하지 않습니다.')
        if data.kind in ('reviewed_tax_credit','prior_unrefunded','pre_notified_tax','acquisition_substitute_paid','purchaser_special_paid','card_company_paid','penalty','deferred_import_input') and data.tax_amount<0:
            raise DomainError('VAT_ADJUSTMENT_SIGN','이 항목은 양수 금액과 별도 정정으로 처리합니다.')
        if data.kind in ('zero_other_supply','exempt_supply_adjustment') and data.tax_amount:
            raise DomainError('VAT_ZERO_OR_EXEMPT_TAX','영세·면세 공급 조정에 세액을 임의로 부여하지 않습니다.')
        if not data.reason.strip():raise DomainError('REASON_REQUIRED','조정의 적용 근거를 기록해야 합니다.',422)
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','preparer'))
            period=self.db.get(c,company,'period',data.period_id)
            ident='vatadj_'+digest({'company':company,'period':period['id'],'reference':data.reference})
            if c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='vat_adjustment' AND id=?",(company,ident)).fetchone():
                old=self.db.get(c,company,'vat_adjustment',ident)
                if old['input']!=value:raise DomainError('VAT_ADJUSTMENT_CONFLICT','같은 근거 번호에 다른 금액이 있습니다.')
                return old
            self.service._evidence_check(c,company,data.evidence_ids,required=True)
            refs=self._authorities(c,company,data.authority_ids,period)
            result=self.db.create(c,company,'vat_adjustment',{'input':value,'author':actor.id,'state':'DRAFT','reviewed_by':None,'review_hash':None,'authority_refs':refs},ident=ident)
            self.db.audit(c,company,actor.id,'vat.adjustment_created',ident,{'kind':data.kind,'period':period['id']})
            return result

    def review_adjustment(self,actor,company,ident,data:VatReview):
        if not data.reason.strip():raise DomainError('REASON_REQUIRED','조정 검토 사유가 필요합니다.',422)
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','reviewer'))
            r=self.db.get(c,company,'vat_adjustment',ident)
            if r['author']==actor.id:raise DomainError('SELF_APPROVAL','조정 작성자와 독립된 검토자가 확인해야 합니다.',403)
            self.auth.guard(c,actor,company,('reviewer',))
            if r['state']!='DRAFT' or r['revision']!=data.expected_revision:raise DomainError('STALE_VERSION','조정이 변경되었습니다.')
            p=self.db.get(c,company,'period',r['input']['period_id'])
            if self._authorities(c,company,r['input']['authority_ids'],p)!=r['authority_refs']:raise DomainError('VAT_AUTHORITY_CHANGED','조정 근거가 변경되었습니다.')
            result=self.db.replace(c,company,'vat_adjustment',ident,{**r,'state':'REVIEWED','reviewed_by':actor.id,'review_hash':_adj_hash(r),'review_note':data.reason},r['revision'])
            self.db.audit(c,company,actor.id,'vat.adjustment_reviewed',ident,{'hash':result['review_hash']})
            return result

    def _compile(self,c,company,period):
        journals=self.db.list(c,company,'journal');by_id={j['id']:j for j in journals}
        fields={name:{'supply':0,'tax':0} for name in ('sales_invoice','sales_other','sales_zero_invoice','sales_zero_other','sales_omitted','bad_debt_adjustment','purchase_general','purchase_fixed_asset','purchase_omitted','import_deferred','other_input','nondeductible','exempt_sales')}
        suppliers={};customers={};trace=[];problems=[]
        classified_assets={p['input']['source_journal_id'] for p in self.db.list(c,company,'book_schedule') if p['input']['kind']=='asset' and p['state'] in ('APPROVED','COMPLETE')}
        input_ledger=output_ledger=0
        for j in journals:
            if j['state']!='POSTED' or not period['start']<=j['event_date']<=period['end'] or j.get('current_period_activity') is False:continue
            input_ledger+=sum(l['amount']*(1 if l['side']=='debit' else -1) for l in j['lines'] if l['account']=='vat_input')
            output_ledger+=sum(l['amount']*(1 if l['side']=='credit' else -1) for l in j['lines'] if l['account']=='vat_output')
            root=j;sign=1;seen=set()
            while root.get('reversal_of'):
                if root['id'] in seen or root['reversal_of'] not in by_id:raise DomainError('VAT_REVERSAL_CHAIN','정정 전표의 원거래를 확인할 수 없습니다.')
                seen.add(root['id']);root=by_id[root['reversal_of']];sign*=-1
            if not root.get('transaction_id'):
                if any(l['account'] in ('vat_input','vat_output') for l in j['lines']):problems.append({'code':'VAT_UNMAPPED_JOURNAL','journal_id':j['id']})
                continue
            tx=self.db.get(c,company,'transaction',root['transaction_id'])
            if tx['kind']!='invoice':continue
            if root.get('transaction_revision')!=tx['revision']:problems.append({'code':'VAT_SOURCE_REVISION','journal_id':j['id']});continue
            supply=tx['net']*sign;tax=tx['tax']*sign
            if tx['direction']=='sale':
                slot='sales_invoice' if tx['tax_category']=='standard' else 'sales_zero_invoice' if tx['tax_category']=='zero' else 'exempt_sales'
                party=customers
            elif tx['direction']=='purchase':
                slot='purchase_fixed_asset' if root['id'] in classified_assets else 'purchase_general'
                if tx['tax_category']!='standard':
                    if tax:problems.append({'code':'VAT_CATEGORY_TAX_CONFLICT','journal_id':j['id']})
                    continue
                party=suppliers
                if tx['deductible'] is not True:
                    fields['nondeductible']['supply']+=supply;fields['nondeductible']['tax']+=tax
            else:continue
            fields[slot]['supply']+=supply;fields[slot]['tax']+=tax
            key=tx['counterparty']
            item=party.setdefault(key,{'counterparty':key,'supply':0,'tax':0,'transaction_ids':[],'journal_ids':[],'business_number_verified':False})
            item['supply']+=supply;item['tax']+=tax;item['transaction_ids'].append(tx['id']);item['journal_ids'].append(j['id'])
            trace.append({'journal_id':j['id'],'root_journal_id':root['id'],'transaction_id':tx['id'],'field':slot,'supply':supply,'tax':tax,'evidence_ids':j['evidence_ids']})
        raw_output=fields['sales_invoice']['tax']
        raw_input=fields['purchase_general']['tax']+fields['purchase_fixed_asset']['tax']-fields['nondeductible']['tax']
        if raw_output!=output_ledger or raw_input!=input_ledger:problems.append({'code':'VAT_LEDGER_MISMATCH','calculated_output':raw_output,'ledger_output':output_ledger,'calculated_input':raw_input,'ledger_input':input_ledger})
        totals={'tax_credit':0,'prior_unrefunded':0,'pre_notified_tax':0,'acquisition_substitute_paid':0,'purchaser_special_paid':0,'card_company_paid':0,'penalty':0}
        adjustments=[r for r in self.db.list(c,company,'vat_adjustment') if r['input']['period_id']==period['id']]
        slots={'output_omitted':'sales_omitted','bad_debt_output_adjustment':'bad_debt_adjustment','input_omitted':'purchase_omitted','deferred_import_input':'import_deferred','other_deductible_input':'other_input','nondeductible_input':'nondeductible','zero_other_supply':'sales_zero_other','exempt_supply_adjustment':'exempt_sales'}
        for r in adjustments:
            v=r['input']
            if r['state']!='REVIEWED' or not r['reviewed_by'] or r['reviewed_by']==r['author'] or r['review_hash']!=_adj_hash(r):problems.append({'code':'VAT_ADJUSTMENT_UNREVIEWED','id':r['id']});continue
            try:
                self.auth.guard(c,Actor(r['reviewed_by'],'vat-adjustment-reviewer'),company,('reviewer',))
                self.service._evidence_check(c,company,v['evidence_ids'],required=True)
                if self._authorities(c,company,v['authority_ids'],period)!=r['authority_refs']:raise DomainError('VAT_AUTHORITY_CHANGED','근거 변경')
            except DomainError as error:problems.append({'code':error.code,'id':r['id']});continue
            if v['kind'] in slots:
                slot=slots[v['kind']];fields[slot]['supply']+=v['supply_amount'];fields[slot]['tax']+=v['tax_amount']
            else:totals['tax_credit' if v['kind']=='reviewed_tax_credit' else v['kind']]+=v['tax_amount']
            trace.append({'adjustment_id':r['id'],'kind':v['kind'],'supply':v['supply_amount'],'tax':v['tax_amount'],'evidence_ids':v['evidence_ids'],'authority_refs':r['authority_refs']})
        output=sum(fields[k]['tax'] for k in ('sales_invoice','sales_other','sales_zero_invoice','sales_zero_other','sales_omitted','bad_debt_adjustment'))
        deductible=fields['purchase_general']['tax']+fields['purchase_fixed_asset']['tax']+fields['purchase_omitted']['tax']-fields['import_deferred']['tax']+fields['other_input']['tax']-fields['nondeductible']['tax']
        net=output-deductible
        credit=min(max(net,0),totals['tax_credit'])
        excess_credit=totals['tax_credit']-credit
        if excess_credit:problems.append({'code':'VAT_CREDIT_LIMIT_REVIEW','amount':excess_credit})
        final=net-credit-totals['prior_unrefunded']-totals['pre_notified_tax']-totals['acquisition_substitute_paid']-totals['purchaser_special_paid']-totals['card_company_paid']+totals['penalty']
        return {'fields':fields,'totals':{**totals,'output_vat':output,'deductible_input_vat':deductible,'net_vat_before_credits':net,'applied_credit':credit,'tax_due_or_refund_before_payment_rounding':final,'positive_is_payable':True},
                'sales_party_summary':list(customers.values()),'purchase_party_summary':list(suppliers.values()),'trace':trace,'blockers':problems,
                'ledger_comparison':{'output':output_ledger,'input':input_ledger,'agrees_before_adjustments':raw_output==output_ledger and raw_input==input_ledger},
                'adjustment_basis':[(r['id'],r['revision'],digest(r)) for r in adjustments]}

    def build(self,actor,company,data:VatBuild):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','preparer','reviewer'))
            p=self.db.get(c,company,'period',data.period_id)
            if p['start']<'2026-01-02' or p['end']>'2026-12-31':raise DomainError('VAT_RULE_DATE','현재 규칙 적용기간을 검토해야 합니다.')
            computed=self._compile(c,company,p)
            readiness=self.service._readiness(c,company,p)
            blockers=computed['blockers']+readiness['blockers']
            if not p['closed']:blockers.append({'code':'VAT_PERIOD_NOT_CLOSED','message':'마감 전 검토용 집계입니다.'})
            source_hash=self.service._snapshot(c,company,p)
            value={**computed,'blockers':blockers,'period_id':p['id'],'start':p['start'],'end':p['end'],'source_hash':source_hash,
                   'rules_version':'kr-general-corporate-vat-workpaper/2026-09-29','rule_source':FLOW,
                   'author':actor.id,'state':'DRAFT','reviewed_by':None,'official_form':False,'live_submission_ready':False,
                   'limitations':['원화·확정 원장·검토된 조정의 신고 검토표','세목별 공제 요건·최신 법정 서식/전자신고 검증 별도','거래처 사업자번호와 실제 증빙 진위는 별도 확인','납부 단위 끝수 처리 전 금액']}
            ident='vatreturn_'+digest({'company':company,'period':p['id'],'source':source_hash,'adjustments':computed['adjustment_basis'],'rules':value['rules_version']})
            if c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='vat_return' AND id=?",(company,ident)).fetchone():return self.db.get(c,company,'vat_return',ident)
            result=self.db.create(c,company,'vat_return',value,ident=ident)
            self.db.audit(c,company,actor.id,'vat.return_workpaper_built',ident,{'source_hash':source_hash,'official_form':False})
            return result

    def review(self,actor,company,ident,data:VatReview):
        if not data.reason.strip():raise DomainError('REASON_REQUIRED','검토 사유가 필요합니다.',422)
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','reviewer'))
            r=self.db.get(c,company,'vat_return',ident)
            if actor.id==r['author']:raise DomainError('SELF_APPROVAL','작성자와 다른 검토자가 확인해야 합니다.',403)
            self.auth.guard(c,actor,company,('reviewer',))
            if r['revision']!=data.expected_revision or r['state']!='DRAFT':raise DomainError('STALE_VERSION','검토표가 변경되었습니다.')
            p=self.db.get(c,company,'period',r['period_id']);current=self._compile(c,company,p)
            if self.service._snapshot(c,company,p)!=r['source_hash'] or current['adjustment_basis']!=r['adjustment_basis']:raise DomainError('VAT_SOURCE_CHANGED','원장·자료·조정이 바뀌었습니다. 새 검토표가 필요합니다.')
            if r['blockers'] or current['blockers'] or not self.service._readiness(c,company,p)['ready'] or not p['closed']:
                raise DomainError('VAT_RETURN_BLOCKED','미해결 자료나 마감 조건을 먼저 해결해야 합니다.')
            result=self.db.replace(c,company,'vat_return',ident,{**r,'state':'REVIEWED_WORKPAPER','reviewed_by':actor.id,'review_hash':digest(r),'review_note':data.reason,'reviewed_at':now()},r['revision'])
            self.db.audit(c,company,actor.id,'vat.workpaper_reviewed',ident,{'official_submission_performed':False})
            return result

    def list(self,actor,company):
        with self.db.connect() as c:
            self.auth.guard(c,actor,company)
            return self.db.list(c,company,'vat_return')
