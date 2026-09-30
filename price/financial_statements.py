"""Reconciled financial statements from the posted ledger and verified openings."""
from __future__ import annotations
from collections import defaultdict
from datetime import date
from .accounts import ACCOUNT_INFO
from .db import DomainError,digest
from .finance_ops import uploaded_source_blockers


class FinancialStatements:
    def __init__(self,service):self.service,self.db,self.auth=service,service.db,service.auth

    def build(self,actor,company,start:date,end:date):
        if type(start) is not date or type(end) is not date or start>end:
            raise DomainError('PERIOD_RANGE','보고 기간이 올바르지 않습니다.',422)
        with self.db.connect() as c:
            self.auth.guard(c,actor,company)
            c.execute('BEGIN')
            all_journals=[j for j in self.db.list(c,company,'journal') if j['state']=='POSTED' and j['event_date']<=end.isoformat()]
            current=[j for j in all_journals if start.isoformat()<=j['event_date'] and j.get('current_period_activity') is not False]
            sums=defaultdict(int);periods=defaultdict(int);dr=cr=0
            for j in all_journals:
                for line in j['lines']:
                    if line['account'] not in ACCOUNT_INFO:raise DomainError('ACCOUNT_MAPPING_MISSING','이전 장부 계정의 보고 분류가 필요합니다.')
                    sums[line['account']]+=line['amount']*(1 if line['side']=='debit' else -1)
                    if line['side']=='debit':dr+=line['amount']
                    else:cr+=line['amount']
            for j in current:
                for line in j['lines']:periods[line['account']]+=line['amount']*(1 if line['side']=='debit' else -1)
            items=[]
            for account,value in sorted(sums.items()):
                name,kind=ACCOUNT_INFO[account]
                displayed=-value if kind in ('income','liability','equity','contra_asset') else value
                items.append({'account':account,'name':name,'category':kind,'balance':displayed,'debit_minus_credit':value})
            asset=sum(sums[k] for k,v in ACCOUNT_INFO.items() if v[1] in ('asset','contra_asset'))
            liability=-sum(sums[k] for k,v in ACCOUNT_INFO.items() if v[1]=='liability')
            capital=-sum(sums[k] for k,v in ACCOUNT_INFO.items() if v[1]=='equity')
            earned=-sum(sums[k] for k,v in ACCOUNT_INFO.items() if v[1] in ('income','expense'))
            income=-sum(periods[k] for k,v in ACCOUNT_INFO.items() if v[1]=='income')
            expense=sum(periods[k] for k,v in ACCOUNT_INFO.items() if v[1]=='expense')
            opening=next((j for j in all_journals if j.get('opening_batch_id') and j['event_date']<=start.isoformat()),None)
            if opening and any(j.get('reversal_of')==opening['id'] for j in all_journals):opening=None
            opening_cash=sum(l['amount']*(1 if l['side']=='debit' else -1) for j in all_journals if j['event_date']<start.isoformat() or j.get('opening_batch_id') for l in j['lines'] if l['account']=='cash') if opening else None
            from .journal_lineage import internal_entry
            lineage={j['id']:j for j in all_journals}
            transfers=[j for j in current if internal_entry(j,lineage)]
            internal_ids={j['id'] for j in transfers}
            external=[j for j in current if j['id'] not in internal_ids]
            inflow=sum(l['amount'] for j in external for l in j['lines'] if l['account']=='cash' and l['side']=='debit')
            outflow=sum(l['amount'] for j in external for l in j['lines'] if l['account']=='cash' and l['side']=='credit')
            internal_in=sum(l['amount'] for j in transfers for l in j['lines'] if l['account']=='cash' and l['side']=='debit')
            internal_out=sum(l['amount'] for j in transfers for l in j['lines'] if l['account']=='cash' and l['side']=='credit')
            ending=sums['cash'] if opening else None
            balances=asset-liability-capital-earned
            blockers=uploaded_source_blockers(self.db,c,company,{'start':start.isoformat(),'end':end.isoformat()})
            from .book_schedules import due_schedule_blockers
            from .inventory_books import inventory_blockers
            range_info={'start':start.isoformat(),'end':end.isoformat()}
            blockers.extend(due_schedule_blockers(self.db,c,company,range_info))
            blockers.extend(inventory_blockers(self.db,c,company,range_info))
            recorded_tx={j.get('transaction_id') for j in all_journals if j.get('transaction_id')}
            for tx in self.db.list(c,company,'transaction'):
                if start.isoformat()<=tx['event_date']<=end.isoformat() and tx['id'] not in recorded_tx:
                    blockers.append({'code':'UNPOSTED_TRANSACTION','id':tx['id'],'message':'장부에 아직 반영되지 않은 거래가 있습니다.'})
            for plan in self.db.list(c,company,'compensation'):
                if plan['state']!='POSTED' and plan['calculation']['paid_on']<=end.isoformat():
                    blockers.append({'code':'COMPENSATION_NOT_POSTED','id':plan['id'],'message':'검토 또는 장부 반영 전인 보수 지급이 있습니다.'})
            covered=any(p['closed'] and p['start']<=start.isoformat() and p['end']>=end.isoformat() for p in self.db.list(c,company,'period'))
            if not covered:blockers.append({'code':'REPORT_PERIOD_NOT_CLOSED','message':'아직 마감되지 않은 기간의 잠정 보고서입니다.'})
            if not opening:blockers.append({'code':'OPENING_BALANCE_NOT_VERIFIED','message':'전기말 잔액 검토 전에는 전체 회사 잔액으로 확정하지 않습니다.'})
            if dr!=cr or balances:blockers.append({'code':'LEDGER_NOT_BALANCED','message':'원장의 차변·대변 또는 재무상태표가 일치하지 않습니다.'})
            audit=self.db.verify_audit(c,company)
            if not audit['valid']:blockers.append({'code':'AUDIT_INVALID','message':'감사 이력의 무결성이 확인되지 않습니다.'})
            return {'period':{'start':start.isoformat(),'end':end.isoformat()},'currency':'KRW',
                    'balance_sheet':{'assets':asset,'liabilities':liability,'capital':capital,'accumulated_profit_in_ledger':earned,'equity_including_profit':capital+earned,'difference':balances,'balanced':balances==0},
                    'income_statement':{'income':income,'expenses':expense,'net_profit':income-expense},
                    'cash':{'opening':opening_cash,'received':inflow,'paid':outflow,'net_movement':inflow-outflow,'closing':ending,'opening_verified':bool(opening),'reconciles':opening_cash+inflow-outflow+internal_in-internal_out==ending if opening else None,'internal_transfers':{'received':internal_in,'paid':internal_out,'net_movement':internal_in-internal_out}},
                    'accounts':items,'trial_balance':{'debit':dr,'credit':cr,'difference':dr-cr},
                    'journal_ids':[j['id'] for j in all_journals],'source_hash':digest(all_journals),
                    'blockers':blockers,'period_closed':covered,'book_values_verified':not blockers,'official_tax_return':False,
                    'notice':'확정 전표로 작성한 장부 재무제표입니다. 기초자료·모든 원본의 완전성과 신고용 세무조정은 별도로 확인합니다.'}
