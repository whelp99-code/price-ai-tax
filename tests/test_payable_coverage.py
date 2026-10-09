"""Isolated original-source payable coverage, not actual Nexias acceptance."""
import hashlib
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from price.api import create_app
from price.casework import Casework
from price.db import DomainError
from price.event_settlements import EventSettlements
from price.extractions import Extractions, ReprocessInput
from price.finance_ops import FinanceOps
from price.ingestion import Ingestion
from price.source_tables import parse_source_table
from tests.conftest import PASSWORD
from tests.test_casework import setup_sources
from tests.test_event_settlements import data_for
from tests.test_owner_upload_api import send, start

PO_HEADER = (
    '발주일자,발주번호,프로젝트ID,공급자사업자등록번호,'
    '공급받는자사업자등록번호,발주금액(원),발주상태,지급조건,납품상태,청구상태\n'
)
PO = (
    PO_HEADER
    + '2026-09-01,SYNTHETIC-PO-01,SYNTHETIC-PROJECT-01,111-11-11111,'
    '222-22-22222,1100,issued,delivery_then_claim,unknown,unknown\n'
).encode()
INVOICE = (
    '작성일자,승인번호,공급자사업자등록번호,공급받는자사업자등록번호,'
    '공급가액(원),세액,합계금액,발주참조번호,프로젝트ID\n'
    '2026-09-02,SYNTHETIC-INVOICE-01,111-11-11111,222-22-22222,'
    '600,60,660,SYNTHETIC-PO-01,SYNTHETIC-PROJECT-01\n'
).encode()


def cash_sources():
    return (
        '계좌번호: 123456789012\n통화: KRW\n'
        '거래일자,거래내용,입금액,출금액,거래번호,상대방사업자등록번호,'
        '청구참조번호,발주참조번호,프로젝트ID,자금성격\n'
        '2026-09-01,합성 선급,0,200,SYNTHETIC-ADV-01,111-11-11111,'
        ',SYNTHETIC-PO-01,SYNTHETIC-PROJECT-01,advance\n'
        '2026-09-03,합성 부분지급,0,300,SYNTHETIC-PAY-01,111-11-11111,'
        'SYNTHETIC-INVOICE-01,SYNTHETIC-PO-01,SYNTHETIC-PROJECT-01,payment\n'
        '2026-09-03,합성 수수료,0,10,SYNTHETIC-FEE-01,111-11-11111,'
        ',SYNTHETIC-PO-01,SYNTHETIC-PROJECT-01,fee\n'
        '2026-09-04,합성 환급,50,0,SYNTHETIC-REFUND-01,111-11-11111,'
        ',SYNTHETIC-PO-01,SYNTHETIC-PROJECT-01,refund\n'
    ).encode()


def coverage(w, documents):
    work = setup_sources(w, documents=documents)
    with w.db.connect() as c:
        states = c.execute('SELECT state,result FROM upload_documents WHERE company_id=?', (w.company,)).fetchall()
        assert all(row['state'] == 'NORMALIZED' for row in states), [
            (row['state'], json.loads(row['result']).get('issues')) for row in states
        ]
    links = EventSettlements(w.svc)
    links.reconcile_strong_references(w.owner, w.company)
    return work, links.report(w.owner, w.company)


def test_issued_po_is_read_with_original_field_provenance():
    result = parse_source_table(PO)
    assert result['state'] == 'NORMALIZED'
    records = result['records']
    assert isinstance(records, list)
    record = records[0]
    assert record['kind'] == 'purchase_order'
    assert record['values']['reference'] == 'SYNTHETIC-PO-01'
    assert record['fields']['gross']['raw'] == '1100'
    assert record['fields']['project_id']['row'] == 2
    assert result['institution_verified'] is False


def test_unbilled_issued_order_is_covered_but_not_payable_today(w):
    work, report = coverage(w, [PO])
    row, = report['purchase_obligations']
    assert (row['ordered'], row['invoiced'], row['unbilled']) == (1100, 0, 1100)
    assert row['state'] == 'UNBILLED_ORDER'
    assert row['payable_today'] is None and row['confirmed'] is False
    assert row['source_record_ids'] == work.list(w.owner, w.company)[0]['source_record_ids']
    with w.db.connect() as c:
        assert not w.db.list(c, w.company, 'journal')


def test_po_and_invoice_are_one_obligation_not_two_balances(w):
    _, report = coverage(w, [PO, INVOICE])
    row, = report['purchase_obligations']
    assert (row['ordered'], row['invoiced'], row['unbilled']) == (1100, 660, 440)
    assert row['economic_amount'] == 1100
    assert len(row['invoice_event_ids']) == 1
    assert row['payable_today'] is None


def test_duplicate_sent_po_attachment_is_one_order_with_both_sources(w):
    sent_copy = PO_HEADER.encode() + PO.split(b'\n', 1)[1] + b'\n'
    _, report = coverage(w, [PO, sent_copy, INVOICE])
    row, = report['purchase_obligations']
    assert row['economic_amount'] == 1100
    assert row['invoiced'] == 660
    assert len(row['source_record_ids']) == 3
    assert len(row['document_ids']) == 3


def test_partial_advance_fee_and_refund_have_distinct_source_amounts(w):
    work, report = coverage(w, [PO, INVOICE, cash_sources()])
    row, = report['purchase_obligations']
    assert row['invoice_allocated'] == 300 and row['invoice_remaining'] == 360, (row, work.list(w.owner, w.company))
    assert row['advance_outflows'] == 200
    assert row['fee_outflows'] == 10
    assert row['refund_inflows'] == 50
    assert row['net_linked_cash'] == 450
    assert row['order_cash_residual'] == 650
    assert row['ordered'] == row['net_linked_cash'] + row['order_cash_residual']
    assert row['payable_today'] is None and row['confirmed'] is False


def unclassified_cash_source(missing_column=False):
    header = (
        '계좌번호: 123456789012\n통화: KRW\n'
        '거래일자,거래내용,입금액,출금액,거래번호,상대방사업자등록번호,'
        '청구참조번호,발주참조번호,프로젝트ID'
    )
    row = (
        '2026-09-05,합성 미분류 출금,0,125,SYNTHETIC-UNKNOWN-01,111-11-11111,'
        ',SYNTHETIC-PO-01,SYNTHETIC-PROJECT-01'
    )
    return (header + ('' if missing_column else ',자금성격') + '\n'
            + row + ('' if missing_column else ',') + '\n').encode()


@pytest.mark.parametrize('missing_column', [True, False], ids=['missing-column', 'empty-cell'])
def test_unclassified_po_linked_outflow_is_visible_without_exact_residual(w, missing_column):
    raw = unclassified_cash_source(missing_column)
    work, _ = coverage(w, [PO, INVOICE, cash_sources(), raw])
    bank = next(event for event in work.list(w.owner, w.company)
                if event['values']['reference'] == 'SYNTHETIC-UNKNOWN-01')
    with TestClient(create_app(w.db.path)) as client:
        assert client.post('/api/login', json={'username': w.owner.username, 'password': PASSWORD}).status_code == 200
        response = client.get('/api/companies/' + w.company + '/owner/reports?start=2026-09-01&end=2026-09-30')
        assert response.status_code == 200
        report = response.json()['payable_coverage']
        row, = report['purchase_obligations']
        assert row['order_cash_residual'] is None
        assert row['unresolved_cash_event_ids'] == [bank['id']]
        assert row['unresolved_cash_outflows'] == 125
        assert row['state'] == 'SOURCE_REVIEW' and 'CASH_CLASSIFICATION_REVIEW' in row['issues']
        assert (row['invoice_allocated'], row['advance_outflows'], row['fee_outflows'], row['refund_inflows']) == (300, 200, 10, 50)
        assert row['net_linked_cash'] == 450
        assert row['payable_today'] is None and row['confirmed'] is False and row['cash_coverage_complete'] is False
        assert report['bank_cutoff_verified'] is False
        assert set(bank['source_record_ids']) <= set(row['source_record_ids'])
        assert set(bank['document_ids']) <= set(row['document_ids'])
        assert bank['context_hash'] in row['context_hashes']
        original = client.get('/api/companies/' + w.company + '/documents/' + bank['document_ids'][0])
        assert original.status_code == 200 and original.json()['sha256'] == hashlib.sha256(raw).hexdigest()
        assert response.json()['official_tax_ready'] is False and response.json()['company_data_complete'] is False
    with w.db.connect() as c:
        assert not w.db.list(c, w.company, 'journal')
        assert all(a['payment_id'] != bank['id'] for a in report['allocations'])


@pytest.mark.parametrize('cutoff,unresolved,residual', [
    (date(2026, 9, 4), 0, 650),
    (date(2026, 9, 5), 125, None),
])
def test_unclassified_cash_obeys_exact_as_of_boundary(w, cutoff, unresolved, residual):
    coverage(w, [PO, INVOICE, cash_sources(), unclassified_cash_source()])
    report = EventSettlements(w.svc).report(w.owner, w.company, as_of=cutoff)
    row, = report['purchase_obligations']
    assert row['unresolved_cash_outflows'] == unresolved
    assert row['order_cash_residual'] == residual
    assert bool(row['unresolved_cash_event_ids']) == bool(unresolved)
    assert row['net_linked_cash'] == 450 and report['bank_cutoff_verified'] is False


def test_unknown_order_amount_is_not_zero_or_invoice_confirmed(w):
    unknown = PO.replace(b',1100,issued,', b',,issued,')
    _, report = coverage(w, [unknown, INVOICE])
    row, = report['purchase_obligations']
    assert row['ordered'] is None and row['unbilled'] is None
    assert row['economic_amount'] is None and row['order_cash_residual'] is None
    assert row['invoiced'] == 660 and row['state'] == 'SOURCE_REVIEW'


def test_same_amount_and_name_do_not_bind_invoice_to_order(w):
    unbound = INVOICE.replace(b'SYNTHETIC-PO-01,SYNTHETIC-PROJECT-01', b',')
    _, report = coverage(w, [PO, unbound])
    assert len(report['purchase_obligations']) == 2
    order = next(row for row in report['purchase_obligations'] if row['po_event_id'])
    assert order['invoiced'] == 0 and order['unbilled'] == 1100
    standalone = next(row for row in report['purchase_obligations'] if not row['po_event_id'])
    assert standalone['economic_amount'] == 660


def test_sales_and_draft_orders_do_not_become_purchase_obligations(w):
    sales = PO.replace(b'111-11-11111,222-22-22222', b'222-22-22222,111-11-11111')
    draft = PO.replace(b',issued,', b',draft,').replace(b'SYNTHETIC-PO-01', b'SYNTHETIC-PO-02')
    _, report = coverage(w, [sales, draft])
    assert report['purchase_obligations'] == []


def test_conflicting_duplicate_order_cannot_supply_a_known_balance(w):
    changed = PO.replace(b',1100,issued,', b',2200,issued,')
    _, report = coverage(w, [PO, changed, INVOICE])
    order = next(row for row in report['purchase_obligations'] if row['po_event_id'])
    assert order['ordered'] is None and order['economic_amount'] is None
    assert 'SOURCE_IDENTITY_CONFLICT' in order['issues']


def test_project_reference_mismatch_never_consumes_other_order(w):
    different = INVOICE.replace(b'SYNTHETIC-PROJECT-01', b'SYNTHETIC-PROJECT-02')
    _, report = coverage(w, [PO, different])
    assert len(report['purchase_obligations']) == 2
    order = next(row for row in report['purchase_obligations'] if row['po_event_id'])
    assert order['invoiced'] == 0


def test_owner_report_contains_read_only_coverage_and_foreign_actor_is_denied(w):
    coverage(w, [PO, INVOICE])
    ops = FinanceOps(w.svc)
    report = ops.report(w.owner, w.company, date(2026, 9, 1), date(2026, 9, 30))
    payable = report.get('payable_coverage')
    assert isinstance(payable, dict)
    obligations = payable.get('purchase_obligations')
    assert isinstance(obligations, list) and len(obligations) == 1
    assert payable.get('confirmed') is False
    with pytest.raises(DomainError):
        ops.report(w.other_owner, w.company, date(2026, 9, 1), date(2026, 9, 30))


def test_as_of_cutoff_excludes_later_refund_without_claiming_bank_completeness(w):
    coverage(w, [PO, INVOICE, cash_sources()])
    report = EventSettlements(w.svc).report(w.owner, w.company, as_of=date(2026, 9, 3))
    row, = report['purchase_obligations']
    assert row['refund_inflows'] == 0 and row['net_linked_cash'] == 500
    assert row['order_cash_residual'] == 600
    assert report['as_of'] == '2026-09-03' and report['bank_cutoff_verified'] is False


def test_advance_applied_to_invoice_is_not_counted_twice_as_cash(w):
    work, _ = coverage(w, [PO, INVOICE, cash_sources()])
    events = work.list(w.owner, w.company)
    inv = next(event for event in events if event['kind'] == 'invoice')
    advance = next(event for event in events if event['values'].get('cash_kind') == 'advance')
    links = EventSettlements(w.svc)
    links.allocate(w.reviewer, w.company, data_for(inv, advance, 200, 'synthetic-advance-applied'))
    row, = links.report(w.owner, w.company)['purchase_obligations']
    assert row['invoice_allocated'] == 500 and row['advance_applied'] == 200
    assert row['advance_outflows'] == 200 and row['net_linked_cash'] == 450


def test_fee_with_invoice_reference_is_never_automatic_invoice_payment(w):
    fee = cash_sources().replace(
        b',,SYNTHETIC-PO-01,SYNTHETIC-PROJECT-01,fee',
        b',SYNTHETIC-INVOICE-01,SYNTHETIC-PO-01,SYNTHETIC-PROJECT-01,fee',
    )
    work, report = coverage(w, [PO, INVOICE, fee])
    fee_event = next(event for event in work.list(w.owner, w.company) if event['values'].get('cash_kind') == 'fee')
    assert all(allocation['payment_id'] != fee_event['id'] for allocation in report['allocations'])
    inv = next(event for event in work.list(w.owner, w.company) if event['kind'] == 'invoice')
    with pytest.raises(DomainError) as failure:
        EventSettlements(w.svc).allocate(w.reviewer, w.company, data_for(inv, fee_event, 10, 'synthetic-fee-reject'))
    assert failure.value.code == 'SETTLEMENT_CASH_KIND'


def test_reprocessed_order_is_explicitly_stale_not_a_known_current_balance(w):
    work, _ = coverage(w, [PO, INVOICE])
    order = next(event for event in work.list(w.owner, w.company) if event['kind'] == 'purchase_order')
    reader = Ingestion(w.svc)
    document = reader.uploads.document(w.owner, w.company, order['document_ids'][0])
    Extractions(w.svc).reprocess(w.owner, w.company, document['id'], ReprocessInput(
        expected_revision=document['revision'], idempotency_key='synthetic-order-reparse', reason='isolated parser revision',
    ))
    report = EventSettlements(w.svc).report(w.owner, w.company)
    assert order['id'] in report['stale_event_ids']
    assert not any(row['po_event_id'] == order['id'] for row in report['purchase_obligations'])
    assert all(row['confirmed'] is False and row['state'] == 'SOURCE_REVIEW' for row in report['purchase_obligations'])


def test_partial_order_payment_without_invoice_stays_unbilled_source_coverage(w):
    raw = cash_sources().splitlines()
    direct = b'\n'.join(raw[:5]) + b'\n'
    direct = direct.replace(b'SYNTHETIC-INVOICE-01,', b',')
    _, report = coverage(w, [PO, direct])
    row, = report['purchase_obligations']
    assert row['invoiced'] == 0 and row['unbilled'] == 1100
    assert row['order_payment_outflows'] == 300 and row['advance_outflows'] == 200
    assert row['net_linked_cash'] == 500 and row['order_cash_residual'] == 600
    assert row['payable_today'] is None


def test_original_upload_http_to_owner_report_keeps_source_hashes_and_company_scope(w):
    app = create_app(w.db.path)
    path = '/api/companies/' + w.company
    with TestClient(app) as client:
        login = client.post('/api/login', json={'username': w.owner.username, 'password': PASSWORD})
        assert login.status_code == 200
        client.headers['x-csrf-token'] = login.json()['csrf']
        facts = client.post(path + '/owner/onboarding', json={
            'expected_revision': 0, 'name': '시험 회사 A', 'business_number': '2222222222',
            'mode': 'company', 'confirmed': True,
            'instruments': [{'kind': 'bank', 'identifier': '123456789012', 'currency': 'KRW'}],
        })
        assert facts.status_code == 200
        document_ids = []
        for index, raw in enumerate((PO, INVOICE, cash_sources())):
            batch = start(client, path, raw=raw, key=f'synthetic-payable-http-{index}')
            assert send(client, path, batch, raw=raw).status_code == 200
            finished = client.post(path + '/uploads/' + batch['id'] + '/complete', json={'expected_revision': batch['revision']})
            assert finished.status_code == 200
            document = finished.json()['files'][0]['document']
            stored = client.get(path + '/documents/' + document['id'])
            assert stored.status_code == 200
            assert stored.json()['sha256'] == hashlib.sha256(raw).hexdigest()
            document_ids.append(document['id'])
        while app.state.source_reader.run_once(w.company)['processed']:
            pass
        Casework(w.svc).refresh(w.owner, w.company)
        assert client.post(path + '/event-settlements/reconcile', json={}).status_code == 200
        response = client.get(path + '/owner/reports?start=2026-09-01&end=2026-09-03')
        assert response.status_code == 200
        report = response.json()['payable_coverage']
        row, = report['purchase_obligations']
        assert row['economic_amount'] == 1100 and row['net_linked_cash'] == 500
        assert set(row['document_ids']) == set(document_ids)
        assert report['confirmed'] is False and report['bank_cutoff_verified'] is False
        assert client.get(path + '/journals').json() == []
        profiles = client.get(path + '/support/formats').json()
        assert profiles['accepted_institution_formats'] == []
        assert next(p for p in profiles['development_readers'] if p['kind'] == 'purchase_order')['verification'] == 'synthetic_only'
    with TestClient(app) as foreign:
        assert foreign.post('/api/login', json={'username': w.other_owner.username, 'password': PASSWORD}).status_code == 200
        assert foreign.get(path + '/owner/reports?start=2026-09-01&end=2026-09-03').status_code == 404
