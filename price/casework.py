"""Source-backed economic events, factual questions and conservative matching.

This layer proposes relationships and remembers facts. It cannot authorize,
post journals, change tax decisions or perform external actions.
"""
from __future__ import annotations
import json
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal
from pydantic import Field, StrictInt, StrictStr
from .db import DomainError, canonical, digest, payload
from .upload_models import UploadInput

VERSION = 'source-casework/2'
MAX_SOURCE_RECORDS = 25000


class FactAnswer(UploadInput):
    expected_revision: Annotated[StrictInt, Field(ge=1)]
    expected_fact_revision: Annotated[StrictInt, Field(ge=0)]=0
    context_hash: Annotated[StrictStr, Field(pattern=r'^[a-f0-9]{64}$')]
    question: Literal['business_use','use_type','cash_reason']
    answer: Annotated[StrictStr, Field(min_length=1,max_length=40)]
    idempotency_key: Annotated[StrictStr, Field(pattern=r'^[A-Za-z0-9_-]{8,120}$')]


QUESTIONS = {
    'business_use': {'text':'이 결제는 회사 업무에 사용했나요?', 'choices':{'business':'회사 업무','personal':'개인 사용','unknown':'모르겠어요'}},
    'use_type': {'text':'구입한 물건이나 서비스는 어디에 사용했나요?', 'choices':{'operations':'회사 운영에 사용','resale':'고객에게 납품','asset':'회사에서 계속 사용하는 장비','other':'그 밖의 용도','unknown':'모르겠어요'}},
    'cash_reason': {'text':'이 돈이 들어오거나 나간 이유는 무엇인가요?', 'choices':{'customer_payment':'고객 대금','supplier_payment':'거래처에 지급','card_payment':'카드 사용대금 납부','company_funding':'대표가 회사에 넣거나 회수한 돈','loan':'대출·상환','internal_transfer':'회사 계좌 사이 이체','refund':'반환·환불','other':'다른 사유','unknown':'모르겠어요'}}
}


def normalized_id(value):
    return re.sub(r'[\s-]', '', str(value or '')).upper()


def amount_text(value):
    if value is None or value == '': return None
    try:
        number=Decimal(str(value))
    except InvalidOperation:
        return None
    return format(number.normalize(),'f') if number.is_finite() else None


def economic_values(record):
    v=record['values']
    return {'date':v.get('date'), 'reference':v.get('reference'),
            'gross':amount_text(v.get('gross')), 'net':amount_text(v.get('net')),
            'tax':amount_text(v.get('tax')), 'currency':v.get('currency'),
            'supplier_id':normalized_id(v.get('supplier_id')), 'buyer_id':normalized_id(v.get('buyer_id')),
            'account':normalized_id(v.get('account')), 'card':normalized_id(v.get('card')),
            'status':v.get('status'), 'original_reference':v.get('original_reference'),
            'amendment_code':v.get('amendment_code'), 'document_type_code':v.get('type_code'),
            'settlement_reference':v.get('settlement_reference'),
            'counterparty_id':normalized_id(v.get('counterparty_id')),
            'counterparty_account':normalized_id(v.get('counterparty_account')),
            'direction':v.get('direction')}


def identity_key(record):
    v=economic_values(record);kind=record['kind'];ref=v['reference']
    if kind in ('tax_invoice_list','tax_invoice_xml') and ref and v['supplier_id'] and v['buyer_id']:
        return ['invoice',v['supplier_id'],v['buyer_id'],ref]
    if kind=='card_transactions' and ref and v['card'] and not any(x in v['card'] for x in ('*','X')):
        return ['card',v['card'],(v['date'] or '')[:10],ref,v['status'],v['original_reference']]
    if kind=='bank_statement' and ref and v['account']:
        return ['bank',v['account'],ref]
    # No strong external identity: preserve legitimate same-amount repeated rows.
    return ['source',record['source_hash'],record.get('sheet',''),record['row']]


def event_shape(records,profile):
    records=sorted(records,key=lambda x:x['id']);first=records[0];v=economic_values(first)
    comparisons={digest(economic_values(r)) for r in records}
    identity_conflict=len(comparisons)>1
    number=normalized_id(profile.get('business_number')) if profile else ''
    owned={(x['kind'],normalized_id(x['identifier'])) for x in (profile or {}).get('instruments',[])}
    kind='invoice' if first['kind'] in ('tax_invoice_list','tax_invoice_xml') else 'card' if first['kind']=='card_transactions' else 'bank' if first['kind']=='bank_statement' else 'unknown'
    direction=None;ownership='unconfirmed';counterparty=''
    if kind=='invoice' and number:
        if v['supplier_id']==number and v['buyer_id']!=number:
            direction='sale';ownership='owner_confirmed';counterparty=v['buyer_id']
        elif v['buyer_id']==number and v['supplier_id']!=number:
            direction='purchase';ownership='owner_confirmed';counterparty=v['supplier_id']
    elif kind=='bank' and ('bank',v['account']) in owned and '*' not in v['account'] and 'X' not in v['account']:
        ownership='owner_confirmed';direction=v['direction'];counterparty=v['counterparty_id']
    elif kind=='card' and ('card',v['card']) in owned:
        # Masked card IDs can be confirmed for context, never used as sole identity proof.
        ownership='masked_owner_statement' if any(x in v['card'] for x in ('*','X')) else 'owner_confirmed'
        direction='purchase';counterparty=v['counterparty_id']
    issues=[]
    if identity_conflict:issues.append('SOURCE_IDENTITY_CONFLICT')
    if not v['currency']:issues.append('CURRENCY_UNCONFIRMED')
    if v['gross'] is None:issues.append('AMOUNT_UNCONFIRMED')
    if ownership=='unconfirmed':issues.append('OWNERSHIP_UNCONFIRMED')
    if v['gross'] is not None and Decimal(v['gross'])<0:issues.append('CORRECTION_REVIEW')
    if v['original_reference'] or any(token in str(v['status'] or '').lower() for token in ('취소','cancel','수정','refund')):issues.append('CORRECTION_REVIEW')
    if any(r['values'].get('amendment_code') for r in records):issues.append('CORRECTION_REVIEW')
    display=first['values'].get('description') or first['values'].get('supplier_name') or first['values'].get('buyer_name') or counterparty or '거래 상대 확인 필요'
    return {'kind':kind,'values':v,'direction':direction,'ownership':ownership,'counterparty_id':counterparty,
            'display_counterparty':display,'source_record_ids':[r['id'] for r in records],
            'evidence_ids':sorted({r['evidence_id'] for r in records}), 'document_ids':sorted({r['document_id'] for r in records}),
            'issues':sorted(set(issues)), 'algorithm_version':VERSION, 'accounting_complete':False,
            'source_signature_verified':False, 'source_statutory_validated':False}


class Casework:
    def __init__(self,service):
        self.service,self.db,self.auth=service,service.db,service.auth

    def _current(self,c,company):
        facts=self.db.list(c,company,'company_facts');profile=facts[0] if facts else None
        rows=c.execute("SELECT o.* FROM objects o JOIN upload_documents d ON d.company_id=o.company_id AND d.id=json_extract(o.body,'$.document_id') WHERE o.company_id=? AND o.kind='source_record' AND COALESCE(json_extract(o.body,'$.lifecycle'),'CURRENT')='CURRENT' AND json_extract(o.body,'$.validation')='NORMALIZED' AND d.state='NORMALIZED' ORDER BY o.id LIMIT ?",(company,MAX_SOURCE_RECORDS+1)).fetchall()
        if len(rows)>MAX_SOURCE_RECORDS:raise DomainError('CASEWORK_LIMIT','자료 규모에 맞는 분할 처리가 필요합니다.')
        records=[self.db.unpack(r) for r in rows if json.loads(r['body']).get('kind') not in ('company_registration','opening_balance')]
        signature=digest({'algorithm_version':VERSION,'profile':profile,'sources':[(r['id'],r['revision'],r.get('generation',1)) for r in records]})
        return records,profile,signature

    def _refresh(self,c,company,actor_id):
        records,profile,signature=self._current(c,company)
        old_state=self.db.list(c,company,'casework_state')
        if old_state and old_state[0]['signature']==signature:
            return {'updated':False,'signature':signature}
        groups=defaultdict(list)
        for record in records:groups[canonical(identity_key(record))].append(record)
        existing={x['id']:x for x in self.db.list(c,company,'economic_event')}
        active=set()
        for key,group in sorted(groups.items()):
            ident='event_'+digest({'company':company,'key':key})
            body=event_shape(group,profile)
            body['identity_key']=json.loads(key)
            body['context_hash']=digest({'economic':body,'profile_revision':profile['revision'] if profile else 0})
            body['lifecycle']='CURRENT'
            current=existing.get(ident)
            if not current:self.db.create(c,company,'economic_event',body,ident=ident)
            elif payload(current)!=body:self.db.replace(c,company,'economic_event',ident,body,current['revision'])
            active.add(ident)
        for ident,current in existing.items():
            if ident not in active and current.get('lifecycle')!='SUPERSEDED':
                self.db.replace(c,company,'economic_event',ident,{**current,'lifecycle':'SUPERSEDED','accounting_complete':False},current['revision'])
        value={'signature':signature,'event_count':len(active),'algorithm_version':VERSION}
        if old_state:self.db.replace(c,company,'casework_state',old_state[0]['id'],value,old_state[0]['revision'])
        else:self.db.create(c,company,'casework_state',value)
        self.db.audit(c,company,actor_id,'casework.refreshed',company,{'events':len(active),'input_hash':signature})
        return {'updated':True,'events':len(active),'signature':signature}

    def refresh(self,actor,company):
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','preparer','reviewer'))
            return self._refresh(c,company,actor.id)

    def run_once(self,company=None):
        with self.db.transaction() as c:
            companies=[r[0] for r in c.execute('SELECT id FROM companies'+(' WHERE id=?' if company else '')+' ORDER BY id',([company] if company else []))]
            for company_id in companies:
                result=self._refresh(c,company_id,'system:casework')
                if result['updated']:return {'processed':True,'state':'PROPOSED','company_id':company_id,'events':result['events']}
        return {'processed':False}

    def _source_context(self,c,company):
        records,profile,_=self._current(c,company)
        return {r['id']:r for r in records},profile

    def _fresh_event(self,c,company,ident,context=None):
        event=self.db.get(c,company,'economic_event',ident)
        if event.get('lifecycle')!='CURRENT':raise DomainError('SOURCE_CHANGED','원본이 다시 처리되었습니다. 최신 자료를 확인하세요.')
        by_id,profile=context if context is not None else self._source_context(c,company)
        group=[by_id[x] for x in event['source_record_ids'] if x in by_id]
        if len(group)!=len(event['source_record_ids']):raise DomainError('SOURCE_CHANGED','자료 변경 이후의 결과를 기다려야 합니다.')
        expected=event_shape(group,profile)
        expected['identity_key']=event['identity_key']
        if digest({'economic':expected,'profile_revision':profile['revision'] if profile else 0})!=event['context_hash']:
            raise DomainError('SOURCE_CHANGED','회사 사실 또는 원본이 바뀌었습니다. 이전 결과로 처리하지 않습니다.')
        return event

    def _answers(self,c,company,event):
        rows=c.execute("SELECT * FROM objects WHERE company_id=? AND kind='event_fact' AND json_extract(body,'$.event_id')=? AND json_extract(body,'$.context_hash')=? AND COALESCE(json_extract(body,'$.active'),1)=1",(company,event['id'],event['context_hash'])).fetchall()
        return {a['question']:a for a in (self.db.unpack(row) for row in rows)}

    def _questions(self,event,answers):
        if event['issues']:return []
        if event['kind'] in ('invoice','card') and event['direction']=='purchase':
            fields=['business_use']
            if answers.get('business_use',{}).get('answer')=='business':fields.append('use_type')
        elif event['kind']=='bank':fields=['cash_reason']
        else:fields=[]
        return [dict(key=key,**QUESTIONS[key]) for key in fields if key not in answers]

    def _view(self,c,company,event,context=None,derived_cash=None):
        answers=self._answers(c,company,event)
        if event['kind']=='card':
            from .card_books import CardBooks
            invoice=CardBooks(self.service).linked_invoice(c,company,event,context)
            if invoice:
                supplied=self._answers(c,company,invoice)
                inherited={k:{**v,'derived_from_sources':True,'source_invoice_id':invoice['id']} for k,v in supplied.items() if k in ('business_use','use_type') and k not in answers}
                answers={**inherited,**answers}
        source_answer=self._source_cash_answer(c,company,event,context,derived_cash)
        if source_answer and 'cash_reason' not in answers:
            answers={**answers,'cash_reason':{'answer':source_answer,'revision':0,'answered_by':None,'derived_from_sources':True}}
        unknown=any(x['answer']=='unknown' for x in answers.values())
        questions=[] if unknown else self._questions(event,answers)
        state='REVIEW_CONNECTION_NEEDED' if unknown or event['issues'] else 'NEEDS_FACTS' if questions else 'FACTS_READY_ACCOUNTING_REVIEW'
        factual=self._answers(c,company,event)
        review_id='review_'+digest({'company':company,'event':event['id'],'context':event['context_hash'],'facts':digest(factual)})
        review_row=c.execute("SELECT * FROM objects WHERE company_id=? AND kind='expert_review' AND id=?",(company,review_id)).fetchone()
        review=self.db.unpack(review_row) if review_row else None
        assignment=None
        if review and review.get('assignee'):
            active=c.execute("SELECT u.username FROM users u JOIN memberships m ON m.user_id=u.id WHERE u.id=? AND m.company_id=? AND m.role='reviewer' AND u.disabled=0",(review['assignee'],company)).fetchone()
            if active:assignment={'user_id':review['assignee'],'name':active[0],'state':review['state'],'professional_credential_verified':review.get('professional_credential_verified',False)}
            if assignment and state=='REVIEW_CONNECTION_NEEDED':state='IN_REVIEW' if review['state']=='IN_REVIEW' else 'REVIEW_ASSIGNED' if review['state']=='ASSIGNED' else 'REVIEW_RESULT_PENDING_ACCOUNTING'
        return {**event,'facts':{k:v['answer'] for k,v in answers.items()},'fact_revisions':{k:v['revision'] for k,v in answers.items()},'fact_actor_ids':sorted({v['answered_by'] for v in answers.values() if v.get('answered_by')}),'facts_from_sources':[k for k,v in answers.items() if v.get('derived_from_sources')],
                'questions':questions,'state':state,'expert_assignee':assignment,'review_id':review['id'] if review else None,'accounting_complete':False}

    def _source_cash_answer(self,c,company,event,context=None,derived_cash=None):
        if event['kind']!='bank' or event['issues'] or event['values']['currency']!='KRW':return None
        if derived_cash is None:
            from .internal_transfers import InternalTransfers
            derived_cash={ident:'internal_transfer' for pair in InternalTransfers(self.service)._pairs(c,company) for ident in pair['event_ids']}
        if event['id'] in derived_cash:return derived_cash[event['id']]
        card_rows=c.execute("SELECT * FROM objects WHERE company_id=? AND kind='card_book' AND json_extract(body,'$.input.operation')='billing' AND json_extract(body,'$.input.source_event_id')=?",(company,event['id'])).fetchall()
        for row in card_rows:
            plan=self.db.unpack(row)
            if plan['basis']['source_hash']!=event['context_hash']:continue
            from .card_books import CardBooks,CardBookInput
            try:
                if plan['state']=='POSTED':CardBooks(self.service)._posted_plan(c,company,plan['id'])
                elif CardBooks(self.service)._basis(c,company,CardBookInput(**plan['input']))!=plan['basis']:continue
            except DomainError:continue
            return 'card_payment' if event['direction']=='payment' else 'refund'
        amount=Decimal(event['values']['gross']);matched=0
        rows=c.execute("SELECT * FROM objects WHERE company_id=? AND kind='event_settlement' AND json_extract(body,'$.payment_id')=?",(company,event['id'])).fetchall()
        for link in (self.db.unpack(row) for row in rows):
            if link['payment_id']!=event['id'] or link['payment_hash']!=event['context_hash'] or not link.get('automatic'):continue
            try:invoice=self._fresh_event(c,company,link['invoice_id'],context)
            except DomainError:continue
            if invoice['context_hash']!=link['invoice_hash'] or invoice['issues']:continue
            matched+=link['amount']
        if Decimal(matched)!=amount:return None
        return 'customer_payment' if event['direction']=='receipt' else 'supplier_payment'

    def list(self,actor,company):
        with self.db.connect() as c:
            c.execute('BEGIN')
            self.auth.guard(c,actor,company)
            context=self._source_context(c,company)
            from .internal_transfers import InternalTransfers
            derived_cash={ident:'internal_transfer' for pair in InternalTransfers(self.service)._pairs(c,company) for ident in pair['event_ids']}
            output=[]
            for event in self.db.list(c,company,'economic_event'):
                if event.get('lifecycle')!='CURRENT':continue
                try:event=self._fresh_event(c,company,event['id'],context)
                except DomainError:
                    output.append({**event,'state':'SOURCE_CHANGED','questions':[],'facts':{},'accounting_complete':False});continue
                output.append(self._view(c,company,event,context,derived_cash))
            return output

    def answer(self,actor,company,ident,data:FactAnswer):
        data=FactAnswer.model_validate(data)
        if data.answer not in QUESTIONS[data.question]['choices']:
            raise DomainError('FACT_CHOICE','제시된 업무 사실 중 하나를 선택하세요.',422)
        key='answer_'+digest({'company':company,'key':data.idempotency_key})
        request_hash=digest({'event':ident,**data.model_dump()})
        with self.db.transaction() as c:
            self.auth.guard(c,actor,company,('owner','preparer'))
            previous=c.execute("SELECT 1 FROM objects WHERE company_id=? AND kind='fact_answer_request' AND id=?",(company,key)).fetchone()
            if previous:
                old=self.db.get(c,company,'fact_answer_request',key)
                if old['request_hash']!=request_hash or old['actor']!=actor.id:raise DomainError('FACT_KEY_CONFLICT','다른 답변에 사용된 요청 번호입니다.')
                return {'saved':True,'reused':True,'fact_id':old['fact_id']}
            event=self._fresh_event(c,company,ident)
            if event['revision']!=data.expected_revision or event['context_hash']!=data.context_hash:
                raise DomainError('STALE_VERSION','자료가 변경되어 질문을 다시 확인해야 합니다.')
            answers=self._answers(c,company,event)
            allowed={q['key'] for q in self._questions(event,{k:v for k,v in answers.items() if k!=data.question})}
            if data.question not in allowed:raise DomainError('FACT_NOT_REQUESTED','이 거래에 필요하지 않은 질문입니다.',422)
            old=answers.get(data.question)
            if data.expected_fact_revision!=(old['revision'] if old else 0):raise DomainError('FACT_STALE_VERSION','다른 답변이 먼저 저장됐습니다. 현재 사실을 확인한 뒤 정정하세요.')
            body={'event_id':ident,'question':data.question,'answer':data.answer,'context_hash':event['context_hash'],
                  'answered_by':actor.id,'active':True,'basis':'owner_business_fact','tax_decision':False}
            fact=self.db.replace(c,company,'event_fact',old['id'],body,old['revision']) if old else self.db.create(c,company,'event_fact',body)
            # A change of business use retires dependent purpose answers.
            if data.question=='business_use' and old and old['answer']!=data.answer and 'use_type' in answers:
                dependent=answers['use_type'];self.db.replace(c,company,'event_fact',dependent['id'],{**dependent,'active':False},dependent['revision'])
            self.db.create(c,company,'fact_answer_request',{'request_hash':request_hash,'fact_id':fact['id'],'actor':actor.id},ident=key)
            self.db.audit(c,company,actor.id,'event.fact_answered',ident,{'question':data.question,'fact_id':fact['id'],'revision':fact['revision']})
            if data.answer=='unknown':
                from .review_cases import ensure_review_case
                ensure_review_case(self.db,c,company,event,digest(self._answers(c,company,event)),actor.id,'대표가 이 거래의 업무 사실을 알 수 없어 자료 확인과 담당자 검토가 필요합니다.')
            return {'saved':True,'reused':False,'fact_id':fact['id'],'event':self._view(c,company,event)}

    def candidates(self,actor,company,ident):
        with self.db.connect() as c:
            c.execute('BEGIN')
            self.auth.guard(c,actor,company)
            context=self._source_context(c,company)
            target=self._fresh_event(c,company,ident,context)
            if target['issues']:return {'event_id':ident,'candidates':[],'state':'SOURCE_REVIEW'}
            candidates=[]
            for other in self.db.list(c,company,'economic_event'):
                if other['id']==ident or other.get('lifecycle')!='CURRENT' or other['issues']:continue
                try:other=self._fresh_event(c,company,other['id'],context)
                except DomainError:continue
                if {target['kind'],other['kind']}!={'invoice','bank'}:continue
                invoice=target if target['kind']=='invoice' else other;bank=target if target['kind']=='bank' else other
                direction=(invoice['direction']=='sale' and bank['direction']=='receipt') or (invoice['direction']=='purchase' and bank['direction']=='payment')
                if not direction or invoice['values']['currency']!=bank['values']['currency']:continue
                if Decimal(invoice['values']['gross'])<=0 or Decimal(bank['values']['gross'])<=0:continue
                reference=bool(bank['values'].get('settlement_reference') and bank['values']['settlement_reference']==invoice['values']['reference'])
                party=bool(bank['counterparty_id'] and bank['counterparty_id']==invoice['counterparty_id'])
                equal=invoice['values']['gross']==bank['values']['gross']
                if not (reference or party or equal):continue
                candidates.append({'other_event_id':other['id'],'invoice_id':invoice['id'],'payment_id':bank['id'],
                                   'strong_reference':reference,'counterparty_match':party,'same_amount':equal,
                                   'relation':'settles','state':'PROPOSED','automatic_allocation':False,
                                   'reason':'참조번호와 상대방을 확인한 결제 후보' if reference and party else '금액·상대방만으로 확정하지 않은 결제 후보'})
            return {'event_id':ident,'candidates':candidates,'state':'AMBIGUOUS' if len(candidates)>1 else 'PROPOSED' if candidates else 'NO_MATCH'}
