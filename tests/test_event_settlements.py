from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import pytest
from price.event_settlements import EventSettlements,SettlementInput
from price.db import DomainError
from price.ingestion import Ingestion
from price.extractions import Extractions,ReprocessInput
from test_casework import setup_sources,invoice
from test_source_ingestion import BANK,INVOICES


def bank_rows(amounts, *, reference=True):
    header='계좌번호: 123456789012\n통화: KRW\n거래일자,거래내용,입금액,출금액,거래번호,상대방사업자등록번호,청구참조번호\n'
    return (header+''.join(f'2026-09-01,수금,{n},0,BANK-{i},222-22-22222,{"SYNTHETIC-01" if reference else ""}\n' for i,n in enumerate(amounts))).encode()


def data_for(inv,bank,amount,key):
    return SettlementInput(invoice_id=inv['id'],payment_id=bank['id'],invoice_hash=inv['context_hash'],payment_hash=bank['context_hash'],amount=amount,idempotency_key=key,reason='합성 결제 관계 검토')


def test_explicit_reference_and_party_auto_connect_without_journal(w):
    work=setup_sources(w,number='1111111111',documents=[INVOICES,bank_rows([1100])]);links=EventSettlements(w.svc)
    result=links.reconcile_strong_references(w.owner,w.company)
    assert result['new_links']==1 and not result['accounting_posted']
    report=links.report(w.owner,w.company)
    assert all(x['remaining']==0 for x in report['balances'])
    assert report['allocations'][0]['automatic']
    assert links.reconcile_strong_references(w.owner,w.company)['new_links']==0
    with w.db.connect() as c:assert not w.db.list(c,w.company,'journal')


def test_three_partial_payments_preserve_invoice_total(w):
    setup_sources(w,number='1111111111',documents=[INVOICES,bank_rows([400,300,400])]);links=EventSettlements(w.svc)
    assert links.reconcile_strong_references(w.owner,w.company)['new_links']==3
    balances=links.report(w.owner,w.company)['balances']
    assert next(b for b in balances if b['kind']=='invoice')['allocated']==1100
    assert all(b['remaining']==0 for b in balances)


def test_equal_money_without_reference_never_auto_connected(w):
    setup_sources(w,number='1111111111',documents=[INVOICES,bank_rows([1100],reference=False)])
    assert EventSettlements(w.svc).reconcile_strong_references(w.owner,w.company)['new_links']==0


def test_overpayment_without_split_information_remains_unallocated(w):
    setup_sources(w,number='1111111111',documents=[INVOICES,bank_rows([2200])]);links=EventSettlements(w.svc)
    assert links.reconcile_strong_references(w.owner,w.company)['new_links']==0


def test_human_reviewed_allocation_checks_both_limits_and_idempotency(w):
    work=setup_sources(w,number='1111111111',documents=[INVOICES,bank_rows([700])]);links=EventSettlements(w.svc)
    events=work.list(w.owner,w.company);inv=next(e for e in events if e['kind']=='invoice');bank=next(e for e in events if e['kind']=='bank')
    data=data_for(inv,bank,600,'manual-settlement-01')
    a=links.allocate(w.reviewer,w.company,data);b=links.allocate(w.reviewer,w.company,data)
    assert a['id']==b['id'] and b['reused']
    with pytest.raises(DomainError) as error:links.allocate(w.reviewer,w.company,data_for(inv,bank,101,'manual-settlement-02'))
    assert error.value.code=='SETTLEMENT_OVERFLOW'
    assert next(b for b in links.report(w.owner,w.company)['balances'] if b['kind']=='invoice')['remaining']==500


def test_simultaneous_allocation_cannot_exceed_payment(w):
    work=setup_sources(w,number='1111111111',documents=[INVOICES,bank_rows([700])]);links=EventSettlements(w.svc)
    events=work.list(w.owner,w.company);inv=next(e for e in events if e['kind']=='invoice');bank=next(e for e in events if e['kind']=='bank')
    def attempt(index):
        try:links.allocate(w.reviewer,w.company,data_for(inv,bank,600,'concurrent-link-'+str(index)));return 'ok'
        except DomainError as error:return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(attempt,[1,2]))
    assert sorted(result)==['SETTLEMENT_OVERFLOW','ok']


def test_tenant_and_role_enforced_for_review_allocation(w):
    work=setup_sources(w,number='1111111111',documents=[INVOICES,BANK]);links=EventSettlements(w.svc)
    events=work.list(w.owner,w.company);inv=next(e for e in events if e['kind']=='invoice');bank=next(e for e in events if e['direction']=='receipt')
    for actor in [w.viewer,w.owner,w.other_owner]:
        with pytest.raises(DomainError):links.allocate(actor,w.company,data_for(inv,bank,100,'disallowed-actor-01'))
    with pytest.raises(DomainError):links.report(w.other_owner,w.company)


def test_stale_source_allocation_not_counted_after_reprocessing(w):
    work=setup_sources(w,number='1111111111',documents=[INVOICES,bank_rows([1100])]);links=EventSettlements(w.svc)
    links.reconcile_strong_references(w.owner,w.company)
    inv=invoice(work,w);ext=Extractions(w.svc);doc=ext.uploads.document(w.owner,w.company,inv['document_ids'][0])
    ext.reprocess(w.owner,w.company,doc['id'],ReprocessInput(expected_revision=doc['revision'],idempotency_key='reprocess-linked-doc',reason='source changed'))
    assert not links.report(w.owner,w.company)['allocations'][0]['current']


def test_wrong_party_reference_does_not_auto_connect(w):
    raw=bank_rows([1100]).replace(b'222-22-22222',b'333-33-33333')
    setup_sources(w,number='1111111111',documents=[INVOICES,raw])
    assert EventSettlements(w.svc).reconcile_strong_references(w.owner,w.company)['new_links']==0


def test_proposal_worker_links_and_avoids_redundant_cash_question(w):
    work=setup_sources(w,number='1111111111',documents=[INVOICES,bank_rows([1100])]);engine=EventSettlements(w.svc)
    assert engine.run_once(w.company)['state']=='LINKED_NOT_POSTED'
    assert not engine.run_once(w.company)['processed']
    bank=next(e for e in work.list(w.owner,w.company) if e['kind']=='bank')
    assert bank['questions']==[] and bank['facts']['cash_reason']=='customer_payment'
    assert bank['facts_from_sources']==['cash_reason'] and not bank['fact_actor_ids']
    with w.db.connect() as c:assert not w.db.list(c,w.company,'journal')
