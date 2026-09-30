"""Follow immutable reversal ancestry for truthful cash and subledger reports."""
from .db import DomainError


def original_entry(journal,index):
    seen=set();current=journal
    while current.get('reversal_of'):
        if current['id'] in seen:raise DomainError('JOURNAL_LINEAGE_CYCLE','정정 원장 연결에 순환이 있습니다.')
        seen.add(current['id'])
        target=current['reversal_of']
        if target not in index:raise DomainError('JOURNAL_LINEAGE_MISSING','정정 원장의 원본을 확인할 수 없습니다.')
        current=index[target]
    return current


def internal_entry(journal,index):
    root=original_entry(journal,index)
    return bool(root.get('internal_transfer_id') and root.get('external_cash_flow') is False)


def card_instrument(journal,index):
    return original_entry(journal,index).get('card_instrument')


def reversal_blockers(db,c,company,period):
    journals=db.list(c,company,'journal')
    reversed_ids={j.get('reversal_of') for j in journals if j['state']=='POSTED' and j['event_date']<=period['end']}
    blockers=[]
    for p in db.list(c,company,'internal_transfer'):
        if p['state']=='POSTED' and any(i in reversed_ids for i in p['journal_ids']):
            blockers.append({'code':'TRANSFER_REVERSAL_REVIEW','id':p['id'],'message':'정정한 회사 계좌 이체의 양쪽 원본과 장부를 다시 대조해야 합니다.'})
    for p in db.list(c,company,'card_book'):
        if p['state']=='POSTED' and p['journal_id'] in reversed_ids:
            blockers.append({'code':'CARD_REVERSAL_REVIEW','id':p['id'],'message':'정정한 카드채무와 원매입·납부의 연결을 다시 대조해야 합니다.'})
    return blockers
