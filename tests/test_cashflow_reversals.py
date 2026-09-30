from datetime import date
from price.financial_statements import FinancialStatements
from price.card_books import CardBooks
from price.finance_ops import FinanceOps
from test_internal_transfers import reviewed as transfer_reviewed
from test_card_books import charge,CARD


def reverse(w,journal_id):
    with w.db.connect() as c:original=w.db.get(c,w.company,'journal',journal_id)
    rev=w.svc.reverse_journal(w.owner,w.company,journal_id,'2026-09-04','합성 잘못된 대사 정정',original['revision'])
    rev=w.svc.transition_journal(w.owner,w.company,rev['id'],'validate',rev['revision'])
    rev=w.svc.transition_journal(w.reviewer,w.company,rev['id'],'approve',rev['revision'],'독립 정정 검토')
    return w.svc.transition_journal(w.owner,w.company,rev['id'],'post',rev['revision'])


def test_internal_transfer_reversal_not_external_cash_receipt(w):
    engine,p=transfer_reviewed(w);posted=engine.post(w.owner,w.company,p['id'])
    reverse(w,posted['journal_ids'][0])
    report=FinancialStatements(w.svc).build(w.owner,w.company,date(2026,9,1),date(2026,9,30))
    assert report['cash']['received']==report['cash']['paid']==0
    assert any(b['code']=='TRANSFER_REVERSAL_REVIEW' for b in report['blockers'])
    status=engine.list(w.owner,w.company)['items'][0]
    assert status['state']=='REVERSAL_REVIEW' and not status['accounting_complete']


def test_reversing_both_internal_legs_preserves_history_and_zero_net(w):
    engine,p=transfer_reviewed(w);posted=engine.post(w.owner,w.company,p['id'])
    for ident in posted['journal_ids']:reverse(w,ident)
    report=FinanceOps(w.svc).report(w.owner,w.company,date(2026,9,1),date(2026,9,30))
    assert report['cash']['received']==report['cash']['paid']==0
    assert report['cash']['internal_transfers']['net_movement']==0
    with w.db.connect() as c:assert len(w.db.list(c,w.company,'journal'))==4


def test_card_balance_reflects_actual_reversal_not_stale_plan(w):
    engine,p,event,original=charge(w)
    with w.db.connect() as c:posted=w.db.get(c,w.company,'card_book',p['id'])
    reverse(w,posted['journal_id'])
    report=engine.list(w.owner,w.company)
    assert report['balances'][CARD]==0
    assert report['items'][0]['source_review_needed']
    with w.db.connect() as c:assert w.db.get(c,w.company,'journal',original['id'])==original
