"""Source-backed FIFO stock ledger, cost-of-sales and exact full returns.

A receipt ties quantities to an already posted inventory acquisition. Receipt
registration does not post a second acquisition. Verified deliveries post COGS
once, never sales/VAT again. All mutable stock balances and journals share one
SQLite transaction; independently reviewed stale allocations are rejected.
"""
from __future__ import annotations
from collections import defaultdict
from datetime import date
from typing import Annotated
from pydantic import Field,StrictInt,StrictStr
from .upload_models import UploadInput
from .auth import Actor
from .db import DomainError,digest,now
from .calculations import validate_lines

Text=Annotated[StrictStr,Field(min_length=1,max_length=160)]
Reference=Annotated[StrictStr,Field(pattern=r'^[A-Za-z0-9가-힣_.:-]{3,120}$')]
Quantity=Annotated[StrictInt,Field(ge=1,le=1_000_000_000)]

class StockReceipt(UploadInput):
    source_journal_id:Text
    sku:Text
    quantity:Quantity
    received_on:date
    evidence_ids:Annotated[list[StrictStr],Field(min_length=1,max_length=30)]
    reference:Reference

class StockIssue(UploadInput):
    sale_journal_id:Text
    sku:Text
    quantity:Quantity
    shipped_on:date
    evidence_ids:Annotated[list[StrictStr],Field(min_length=1,max_length=30)]
    reference:Reference

class StockReview(UploadInput):
    expected_revision:Annotated[StrictInt,Field(ge=1)]
    reason:Annotated[StrictStr,Field(min_length=1,max_length=2000)]

class StockReturn(UploadInput):
    issue_id:Text
    returned_on:date
    evidence_ids:Annotated[list[StrictStr],Field(min_length=1,max_length=30)]
    reference:Reference


def _amount(journal,account):
    return sum(x['amount']*(1 if x['side']=='debit' else -1) for x in journal['lines'] if x['account']==account)


class InventoryBooks:
    def __init__(self,service,*,today=None):
        self.service,self.db,self.auth=service,service.db,service.auth
        self.today=today or date.today

    def _source(self,c,company,ident,account):
        j=self.db.get(c,company,'journal',ident)
        if j['state']!='POSTED' or j.get('reversal_of'):raise DomainError('STOCK_SOURCE','확정된 매입·판매 원본 전표가 필요합니다.')
        amount=_amount(j,account)*(1 if account=='inventory' else -1)
        if amount<=0:raise DomainError('STOCK_SOURCE','원장에 해당 재고 취득원가·판매가 확인되지 않습니다.')
        if any(r.get('reversal_of')==ident and r['state']=='POSTED' for r in self.db.list(c,company,'journal')):raise DomainError('STOCK_SOURCE_REVERSED','원매입·판매가 정정되어 재고 연결 검토가 필요합니다.')
        if any(ident in x.get('posted_journal_ids',[]) and x.get('state')=='REVIEW_REQUIRED' for x in self.db.list(c,company,'source_change_impact')):raise DomainError('STOCK_SOURCE_CHANGED','원본 재처리 영향을 먼저 검토해야 합니다.')
        self.service._evidence_check(c,company,j['evidence_ids'],required=True)
        return j,amount

    def _dated(self,c,company,when,sku):
        if when>self.today():raise DomainError('STOCK_FUTURE','미래 입출고를 확정하지 않습니다.')
        self.service._locked(c,company,when.isoformat())
        movements=[m for m in self.db.list(c,company,'stock_movement') if m['sku']==sku]
        if any(m['event_date']>when.isoformat() for m in movements):raise DomainError('STOCK_BACKDATED','후속 재고 이동보다 이른 입출고는 원가 재검토가 필요합니다.')

    def _receipt_hash(self,lot):
        return digest({k:lot[k] for k in ('input','source_hash','original_cost','author')})

    def receive(self,actor,company,data:StockReceipt):
        data=StockReceipt.model_validate(data);values=data.model_dump(mode='json');key='lot_'+digest({'company':company,'reference':data.reference})
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','preparer'))
            if c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='stock_lot' AND id=?",(company,key)).fetchone():
                old=self.db.get(c,company,'stock_lot',key)
                if old['input']!=values:raise DomainError('STOCK_REFERENCE_CONFLICT','같은 입고 번호의 수량·원본이 다릅니다.')
                return old
            source,cost=self._source(c,company,data.source_journal_id,'inventory')
            self._dated(c,company,data.received_on,data.sku)
            if source['event_date']>data.received_on.isoformat():raise DomainError('STOCK_SOURCE_DATE','매입 인식 이전 입고의 원가 연결을 검토해야 합니다.')
            self.service._evidence_check(c,company,data.evidence_ids,required=True)
            if any(x['input']['source_journal_id']==source['id'] for x in self.db.list(c,company,'stock_lot')):
                raise DomainError('STOCK_SOURCE_DUPLICATE','같은 매입원가를 중복 입고에 배정하지 않습니다.')
            result=self.db.create(c,company,'stock_lot',{'input':values,'source_hash':digest(source),'original_cost':cost,
                    'original_quantity':data.quantity,'remaining_quantity':data.quantity,'remaining_cost':cost,
                    'basis_quantity':data.quantity,'basis_cost':cost,'author':actor.id,'state':'DRAFT','reviewed_by':None,'review_hash':None},ident=key)
            self.db.audit(c,company,actor.id,'stock.receipt_proposed',key,{'cost':cost,'quantity':data.quantity,'duplicate_acquisition_posted':False})
            return result

    def _independent(self,c,company,record,actor,data):
        self.auth.guard(c,actor,company,('owner','reviewer'))
        if record['author']==actor.id:raise DomainError('SELF_APPROVAL','입출고 작성자와 다른 검토자가 확인해야 합니다.',403)
        self.auth.guard(c,actor,company,('reviewer',))
        if record['state']!='DRAFT' or record['revision']!=data.expected_revision:raise DomainError('STALE_VERSION','입출고 계획이 변경되었습니다.')
        if not data.reason.strip():raise DomainError('REASON_REQUIRED','검토 근거를 기록하세요.',422)

    def approve_receipt(self,actor,company,ident,data:StockReview):
        with self.db.transaction() as c:
            lot=self.db.get(c,company,'stock_lot',ident);self._independent(c,company,lot,actor,data)
            source,_=self._source(c,company,lot['input']['source_journal_id'],'inventory')
            if digest(source)!=lot['source_hash']:raise DomainError('STOCK_SOURCE_CHANGED','매입 원가가 변경되었습니다.')
            self._dated(c,company,date.fromisoformat(lot['input']['received_on']),lot['input']['sku'])
            lot=self.db.replace(c,company,'stock_lot',ident,{**lot,'state':'ACTIVE','reviewed_by':actor.id,'review_hash':self._receipt_hash(lot),'review_note':data.reason},lot['revision'])
            self.db.create(c,company,'stock_movement',{'lot_id':ident,'sku':lot['input']['sku'],'kind':'RECEIPT','quantity':lot['original_quantity'],'cost':lot['original_cost'],'event_date':lot['input']['received_on'],'source_journal_id':source['id']})
            self.db.audit(c,company,actor.id,'stock.receipt_confirmed',ident,{'acquisition_already_posted':True})
            return lot

    def _lot_valid(self,c,company,lot):
        if lot['state']!='ACTIVE' or lot['review_hash']!=self._receipt_hash(lot):raise DomainError('STOCK_RECEIPT_UNREVIEWED','입고 원가의 독립 검토가 필요합니다.')
        self.auth.guard(c,Actor(lot['reviewed_by'],'stock-reviewer'),company,('reviewer',))
        source,_=self._source(c,company,lot['input']['source_journal_id'],'inventory')
        if digest(source)!=lot['source_hash']:raise DomainError('STOCK_SOURCE_CHANGED','재고 매입원본이 변경되었습니다.')

    def _fifo(self,c,company,data):
        source,_=self._source(c,company,data['sale_journal_id'],'revenue')
        shipped=date.fromisoformat(data['shipped_on']);self._dated(c,company,shipped,data['sku'])
        if source['event_date']>data['shipped_on']:raise DomainError('STOCK_SALE_DATE','판매 인식 이전 출고 연결을 검토해야 합니다.')
        self.service._evidence_check(c,company,data['evidence_ids'],required=True)
        lots=sorted((l for l in self.db.list(c,company,'stock_lot') if l['input']['sku']==data['sku'] and l['state']=='ACTIVE' and l['input']['received_on']<=data['shipped_on'] and l['remaining_quantity']),key=lambda l:(l['input']['received_on'],l['id']))
        remaining=data['quantity'];parts=[]
        for lot in lots:
            self._lot_valid(c,company,lot)
            taken=min(remaining,lot['remaining_quantity'])
            consumed=lot['basis_quantity']-lot['remaining_quantity'];old_cost=lot['basis_cost']-lot['remaining_cost']
            cost=lot['basis_cost']*(consumed+taken)//lot['basis_quantity']-old_cost
            if not 0<=cost<=lot['remaining_cost']:raise DomainError('STOCK_COST_CORRUPT','재고 원가 누계가 맞지 않습니다.')
            parts.append({'lot_id':lot['id'],'revision':lot['revision'],'lot_hash':digest(lot),'quantity':taken,'cost':cost})
            remaining-=taken
            if not remaining:break
        if remaining:raise DomainError('INSUFFICIENT_STOCK','확인된 보유 재고보다 출고 수량이 큽니다.')
        return {'source_hash':digest(source),'parts':parts,'cost':sum(x['cost'] for x in parts)}

    def issue(self,actor,company,data:StockIssue):
        values=StockIssue.model_validate(data).model_dump(mode='json')
        key='issue_'+digest({'company':company,'reference':data.reference,'sku':data.sku})
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','preparer'))
            if c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='stock_issue' AND id=?",(company,key)).fetchone():
                old=self.db.get(c,company,'stock_issue',key)
                if old['input']!=values:raise DomainError('STOCK_REFERENCE_CONFLICT','같은 출고 번호의 내용이 다릅니다.')
                return old
            basis=self._fifo(c,company,values)
            value={'input':values,'basis':basis,'cost':basis['cost'],'author':actor.id,'state':'DRAFT','reviewed_by':None,'review_hash':None,'journal_id':None}
            result=self.db.create(c,company,'stock_issue',value,ident=key)
            self.db.audit(c,company,actor.id,'stock.issue_proposed',key,{'quantity':data.quantity,'cost':basis['cost']})
            return result

    def approve_issue(self,actor,company,ident,data:StockReview):
        with self.db.transaction() as c:
            issue=self.db.get(c,company,'stock_issue',ident);self._independent(c,company,issue,actor,data)
            if self._fifo(c,company,issue['input'])!=issue['basis']:raise DomainError('STOCK_ALLOCATION_CHANGED','재고가 변동되어 출고 원가를 다시 계산해야 합니다.')
            h=digest({'input':issue['input'],'basis':issue['basis'],'author':issue['author']})
            result=self.db.replace(c,company,'stock_issue',ident,{**issue,'state':'APPROVED','reviewed_by':actor.id,'review_hash':h,'review_note':data.reason},issue['revision'])
            self.db.audit(c,company,actor.id,'stock.issue_approved',ident,{'hash':h})
            return result

    def _post_journal(self,c,company,when,cost,evidence,debit,credit,actor,reviewer,reference):
        if not cost:return None
        data={'event_date':when,'description':'검토된 재고 원가 '+reference,'evidence_ids':evidence,
              'lines':[{'account':debit,'side':'debit','amount':cost},{'account':credit,'side':'credit','amount':cost}]}
        j=self.service._new_journal(c,Actor('system:stock','stock'),company,data);self.service._journal_valid(c,company,j)
        j.update(state='POSTED',approved_by=reviewer,approval_hash=self.service.journal_hash(j),inventory_reference=reference,authorization_kind='REVIEWED_STOCK_MOVEMENT')
        j=self.db.replace(c,company,'journal',j['id'],j,j['revision'])
        self.db.audit(c,company,actor,'stock.cost_posted',j['id'],{'reference':reference,'cost':cost})
        return j['id']

    def post_issue(self,actor,company,ident):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','preparer','reviewer'))
            issue=self.db.get(c,company,'stock_issue',ident)
            if issue['state']=='POSTED':return issue
            if issue['state']!='APPROVED' or issue['reviewed_by']==issue['author'] or issue['review_hash']!=digest({'input':issue['input'],'basis':issue['basis'],'author':issue['author']}):raise DomainError('STOCK_NOT_APPROVED','검토된 출고만 장부에 반영합니다.')
            self.auth.guard(c,Actor(issue['reviewed_by'],'stock-reviewer'),company,('reviewer',))
            if self._fifo(c,company,issue['input'])!=issue['basis']:raise DomainError('STOCK_ALLOCATION_CHANGED','검토 후 재고가 변동되어 재검토가 필요합니다.')
            for part in issue['basis']['parts']:
                lot=self.db.get(c,company,'stock_lot',part['lot_id'])
                self.db.replace(c,company,'stock_lot',lot['id'],{**lot,'remaining_quantity':lot['remaining_quantity']-part['quantity'],'remaining_cost':lot['remaining_cost']-part['cost']},lot['revision'])
                self.db.create(c,company,'stock_movement',{'lot_id':lot['id'],'sku':lot['input']['sku'],'kind':'ISSUE','quantity':-part['quantity'],'cost':-part['cost'],'event_date':issue['input']['shipped_on'],'issue_id':ident})
            source=self.db.get(c,company,'journal',issue['input']['sale_journal_id'])
            evidence=sorted(set(source['evidence_ids']+issue['input']['evidence_ids']))
            journal=self._post_journal(c,company,issue['input']['shipped_on'],issue['cost'],evidence,'cost_of_sales','inventory',actor.id,issue['reviewed_by'],ident)
            return self.db.replace(c,company,'stock_issue',ident,{**issue,'state':'POSTED','journal_id':journal,'posted_at':now()},issue['revision'])

    def _return_basis(self,c,company,issue_id,when):
        issue=self.db.get(c,company,'stock_issue',issue_id)
        if issue['state']!='POSTED':raise DomainError('RETURN_SOURCE','확정된 출고의 반품만 처리합니다.')
        self._dated(c,company,date.fromisoformat(when),issue['input']['sku'])
        if when<issue['input']['shipped_on']:raise DomainError('RETURN_DATE','출고보다 반품이 빠릅니다.')
        if any(x['input']['issue_id']==issue_id and x['state']=='POSTED' for x in self.db.list(c,company,'stock_return')):raise DomainError('RETURN_DUPLICATE','이 출고의 반품이 이미 반영되었습니다.')
        if issue['journal_id'] and any(j.get('reversal_of')==issue['journal_id'] and j['state']=='POSTED' for j in self.db.list(c,company,'journal')):raise DomainError('RETURN_COST_REVERSED','출고 원가가 이미 정정되어 재고와 함께 검토해야 합니다.')
        for part in issue['basis']['parts']:self._lot_valid(c,company,self.db.get(c,company,'stock_lot',part['lot_id']))
        return issue

    def return_goods(self,actor,company,data:StockReturn):
        values=StockReturn.model_validate(data).model_dump(mode='json');ident='stockreturn_'+digest({'company':company,'reference':data.reference})
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','preparer'))
            if c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='stock_return' AND id=?",(company,ident)).fetchone():
                old=self.db.get(c,company,'stock_return',ident)
                if old['input']!=values:raise DomainError('STOCK_REFERENCE_CONFLICT','같은 반품번호에 다른 내용이 있습니다.')
                return old
            source=self._return_basis(c,company,data.issue_id,data.returned_on.isoformat())
            self.service._evidence_check(c,company,data.evidence_ids,required=True)
            result=self.db.create(c,company,'stock_return',{'input':values,'source_hash':digest(source),'cost':source['cost'],'author':actor.id,'state':'DRAFT','reviewed_by':None,'review_hash':None},ident=ident)
            self.db.audit(c,company,actor.id,'stock.return_proposed',ident,{'issue_id':source['id']})
            return result

    def approve_return(self,actor,company,ident,data:StockReview):
        with self.db.transaction() as c:
            ret=self.db.get(c,company,'stock_return',ident);self._independent(c,company,ret,actor,data)
            source=self._return_basis(c,company,ret['input']['issue_id'],ret['input']['returned_on'])
            if digest(source)!=ret['source_hash']:raise DomainError('RETURN_SOURCE_CHANGED','원출고가 변경되었습니다.')
            h=digest({'input':ret['input'],'source_hash':ret['source_hash'],'cost':ret['cost'],'author':ret['author']})
            result=self.db.replace(c,company,'stock_return',ident,{**ret,'state':'APPROVED','reviewed_by':actor.id,'review_hash':h,'review_note':data.reason},ret['revision'])
            self.db.audit(c,company,actor.id,'stock.return_approved',ident,{'hash':h})
            return result

    def post_return(self,actor,company,ident):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','preparer','reviewer'))
            ret=self.db.get(c,company,'stock_return',ident)
            if ret['state']=='POSTED':return ret
            h=digest({'input':ret['input'],'source_hash':ret['source_hash'],'cost':ret['cost'],'author':ret['author']})
            if ret['state']!='APPROVED' or ret['review_hash']!=h or ret['reviewed_by']==ret['author']:raise DomainError('STOCK_NOT_APPROVED','독립 검토된 반품만 반영합니다.')
            self.auth.guard(c,Actor(ret['reviewed_by'],'stock-reviewer'),company,('reviewer',))
            issue=self._return_basis(c,company,ret['input']['issue_id'],ret['input']['returned_on'])
            if digest(issue)!=ret['source_hash']:raise DomainError('RETURN_SOURCE_CHANGED','원출고가 변경되었습니다.')
            self.service._evidence_check(c,company,ret['input']['evidence_ids'],required=True)
            for part in issue['basis']['parts']:
                lot=self.db.get(c,company,'stock_lot',part['lot_id'])
                quantity=lot['remaining_quantity']+part['quantity'];cost=lot['remaining_cost']+part['cost']
                if quantity>lot['original_quantity'] or cost>lot['original_cost']:raise DomainError('RETURN_EXCEEDS_RECEIPT','반품 수량·원가가 원입고를 초과합니다.')
                self.db.replace(c,company,'stock_lot',lot['id'],{**lot,'remaining_quantity':quantity,'remaining_cost':cost,'basis_quantity':quantity,'basis_cost':cost},lot['revision'])
                self.db.create(c,company,'stock_movement',{'lot_id':lot['id'],'sku':lot['input']['sku'],'kind':'RETURN','quantity':part['quantity'],'cost':part['cost'],'event_date':ret['input']['returned_on'],'return_id':ident,'issue_id':issue['id']})
            journal=self._post_journal(c,company,ret['input']['returned_on'],ret['cost'],ret['input']['evidence_ids'],'inventory','cost_of_sales',actor.id,ret['reviewed_by'],ident)
            return self.db.replace(c,company,'stock_return',ident,{**ret,'state':'POSTED','journal_id':journal,'posted_at':now()},ret['revision'])

    def balance(self,actor,company):
        with self.db.connect() as c:
            self.auth.guard(c,actor,company)
            grouped={}
            for lot in self.db.list(c,company,'stock_lot'):
                if lot['state']!='ACTIVE':continue
                row=grouped.setdefault(lot['input']['sku'],{'sku':lot['input']['sku'],'quantity':0,'value':0,'lot_ids':[],'source_review_needed':False})
                row['quantity']+=lot['remaining_quantity'];row['value']+=lot['remaining_cost'];row['lot_ids'].append(lot['id'])
                try:self._lot_valid(c,company,lot)
                except DomainError:row['source_review_needed']=True
            return {'items':list(grouped.values()),'valuation':'FIFO_integer_cumulative','physical_count_verified':False,'tax_valuation_verified':False,
                    'notice':'확인된 입고·출고·반품으로 계산한 장부 재고입니다. 실사 수량과 세무 평가는 별도 검토합니다.'}


def inventory_blockers(db,c,company,period):
    output=[]
    for kind,datefield,code in [('stock_issue','shipped_on','STOCK_ISSUE_NOT_POSTED'),('stock_return','returned_on','STOCK_RETURN_NOT_POSTED')]:
        for row in db.list(c,company,kind):
            if row['state']!='POSTED' and row['input'][datefield]<=period['end']:
                output.append({'code':code,'id':row['id'],'message':'재고 입출고와 원가 반영의 검토가 필요합니다.'})
    return output
