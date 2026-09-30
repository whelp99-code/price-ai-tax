"""Real double-entry effects from two source-confirmed own bank accounts."""
from datetime import date
from concurrent.futures import ThreadPoolExecutor
import pytest
from price.internal_transfers import InternalTransfers, TransferReview
from price.owner import OwnerView, CompanyFactsInput
from price.ingestion import Ingestion
from price.casework import Casework
from price.db import DomainError
from price.financial_statements import FinancialStatements
from price.extractions import Extractions, ReprocessInput
from test_source_ingestion import upload

A='111111111111'; B='222222222222'


def data(*,amount=1000,other_amount=None,reciprocal=True,extra=False,second_date='2026-09-01',reference=True):
    fields='거래일시,거래내용,입금액,출금액,계좌번호,상대계좌번호,거래번호,통화\n'
    a=f'2026-09-01 10:00:00,자금 이동,0,{amount},{A},{B},OUT-1,KRW\n'
    b=f'{second_date} 10:00:01,자금 이동,{other_amount or amount},0,{B},{A if reciprocal else "999999999999"},IN-1,KRW\n'
    if extra:b+=f'{second_date} 10:00:01,다른 동일금액 입금,{amount},0,{B},{A},IN-2,KRW\n'
    return (fields+a+b).encode()


def setup(w,**options):
    reader=Ingestion(w.svc)
    OwnerView(w.svc,reader.uploads).confirm(w.owner,w.company,CompanyFactsInput(expected_revision=0,name='합성 법인',mode='company',confirmed=True,instruments=[{'kind':'bank','identifier':A,'currency':'KRW'},{'kind':'bank','identifier':B,'currency':'KRW'}]))
    batch=upload(w,data(**options));reader.run_once(w.company)
    Casework(w.svc).refresh(w.owner,w.company)
    engine=InternalTransfers(w.svc,today=lambda:date(2026,9,30));engine.refresh(w.owner,w.company)
    return engine,batch


def reviewed(w):
    e,_=setup(w);p=e.list(w.owner,w.company)['items'][0]
    e.approve(w.reviewer,w.company,p['id'],TransferReview(expected_revision=p['revision'],reason='두 회사 소유 계좌와 상호 참조·금액을 독립 대조'))
    return e,p


def test_source_only_builds_one_plan_without_manual_account_selection(w):
    e,_=setup(w);items=e.list(w.owner,w.company)['items']
    assert len(items)==1 and items[0]['state']=='DRAFT'
    assert items[0]['amount']==1000
    with w.db.connect() as c:assert not w.db.list(c,w.company,'journal')
    events=Casework(w.svc).list(w.owner,w.company)
    assert all(not x['questions'] for x in events)
    assert all(x['facts']['cash_reason']=='internal_transfer' for x in events)
    assert all(not x['accounting_complete'] for x in events)


def test_reviewed_transfer_posts_two_real_immutable_balanced_legs(w):
    e,p=reviewed(w);result=e.post(w.owner,w.company,p['id'])
    assert result['state']=='POSTED' and len(result['journal_ids'])==2
    with w.db.connect() as c:
        journals=w.db.list(c,w.company,'journal');assert len(journals)==2
        assert all(j['state']=='POSTED' and j['internal_transfer_id']==p['id'] for j in journals)
        assert len({j['economic_event_id'] for j in journals})==2
        assert {x['account'] for j in journals for x in j['lines']}=={'cash','transfer_clearing'}
        assert sum(x['amount']*(1 if x['side']=='debit' else -1) for j in journals for x in j['lines'] if x['account']=='transfer_clearing')==0
        assert w.db.verify_audit(c,w.company)['valid']
    assert e.post(w.owner,w.company,p['id'])['journal_ids']==result['journal_ids']


def test_internal_cash_movement_is_not_income_expense_or_external_cash_flow(w):
    e,p=reviewed(w);e.post(w.owner,w.company,p['id'])
    report=FinancialStatements(w.svc).build(w.owner,w.company,date(2026,9,1),date(2026,9,30))
    assert report['income_statement']['income']==report['income_statement']['expenses']==0
    assert report['cash']['received']==report['cash']['paid']==0
    assert report['cash']['internal_transfers']=={'received':1000,'paid':1000,'net_movement':0}
    assert report['cash']['closing'] is None  # no fabricated opening balance


@pytest.mark.parametrize('options',[{'reciprocal':False},{'extra':True},{'other_amount':999},{'second_date':'2026-09-02'}])
def test_no_guessing_from_amount_or_one_sided_reference(w,options):
    e,_=setup(w,**options)
    assert e.list(w.owner,w.company)['items']==[]
    assert all(not x['accounting_complete'] for x in Casework(w.svc).list(w.owner,w.company))


def test_no_self_review_no_viewer_writes_no_cross_company(w):
    e,_=setup(w);p=e.list(w.owner,w.company)['items'][0]
    with pytest.raises(DomainError):e.approve(w.owner,w.company,p['id'],TransferReview(expected_revision=p['revision'],reason='self'))
    with pytest.raises(DomainError):e.post(w.owner,w.company,p['id'])
    with pytest.raises(DomainError):e.refresh(w.viewer,w.company)
    with pytest.raises(DomainError):e.list(w.other_owner,w.company)


def test_source_reprocessing_invalidates_unused_review(w):
    e,p=reviewed(w);ext=Extractions(w.svc);doc=ext.uploads.document(w.owner,w.company,p['document_ids'][0])
    ext.reprocess(w.owner,w.company,doc['id'],ReprocessInput(expected_revision=doc['revision'],idempotency_key='transfer-reprocess',reason='원본 재대조'))
    with pytest.raises(DomainError):e.post(w.owner,w.company,p['id'])
    with w.db.connect() as c:assert not w.db.list(c,w.company,'journal')


def test_new_ambiguous_match_revokes_old_unique_relationship(w):
    e,p=reviewed(w)
    raw=('거래일시,거래내용,입금액,출금액,계좌번호,상대계좌번호,거래번호,통화\n'+f'2026-09-01 10:00:01,추가 입금,1000,0,{B},{A},IN-3,KRW\n').encode()
    upload(w,raw,key='late-transfer-duplicate');Ingestion(w.svc).run_once(w.company);Casework(w.svc).refresh(w.owner,w.company)
    with pytest.raises(DomainError):e.post(w.owner,w.company,p['id'])


def test_reviewer_revocation_and_period_lock_prevent_both_legs(w):
    e,p=reviewed(w)
    with w.db.transaction() as c:c.execute('DELETE FROM memberships WHERE company_id=? AND user_id=?',(w.company,w.reviewer.id))
    with pytest.raises(DomainError):e.post(w.owner,w.company,p['id'])
    with w.db.connect() as c:assert not w.db.list(c,w.company,'journal')


def test_concurrent_post_has_exactly_two_legs(w):
    e,p=reviewed(w)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:e.post(w.owner,w.company,p['id']),range(2)))
    assert results[0]['journal_ids']==results[1]['journal_ids']
    with w.db.connect() as c:assert len(w.db.list(c,w.company,'journal'))==2


def test_source_participant_cannot_approve_worker_generated_plan(w):
    e,p=setup(w);items=e.list(w.owner,w.company)['items'];plan=items[0]
    with w.db.transaction() as c:c.execute("UPDATE memberships SET role='reviewer' WHERE user_id=? AND company_id=?",(w.owner.id,w.company))
    with pytest.raises(DomainError) as error:e.approve(w.owner,w.company,plan['id'],TransferReview(expected_revision=plan['revision'],reason='원본 작성자의 자기 검토'))
    assert error.value.code=='SELF_APPROVAL'
