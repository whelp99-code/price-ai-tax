"""Isolated native read contract; not agent identity or transport acceptance."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from price.api import create_app
from price.auth import Actor
from price.db import DomainError
from price.extractions import Extractions, ReprocessInput
from price.finance_ops import FinanceOps
from price.ingestion import Ingestion
from tests.conftest import PASSWORD
from tests.test_payable_coverage import INVOICE, PO, cash_sources, coverage


@pytest.mark.parametrize('membership', ['owner', 'viewer'])
def test_worker_flag_is_denied_even_with_native_read_membership(w, membership):
    member = getattr(w, membership)
    worker = Actor(member.id, member.username, worker=True)
    with w.db.connect() as c:
        before = w.db.list(c, w.company, 'journal')
        audit = w.db.verify_audit(c, w.company)
    with pytest.raises(DomainError) as failure:
        FinanceOps(w.svc).report(worker, w.company, date(2026, 9, 1), date(2026, 9, 30))
    assert failure.value.code == 'WORKER_FORBIDDEN' and failure.value.status == 403
    with w.db.connect() as c:
        assert w.db.list(c, w.company, 'journal') == before
        assert w.db.verify_audit(c, w.company) == audit


def test_native_viewer_reads_dates_without_tax_or_completeness_acceptance(w):
    coverage(w, [PO, INVOICE, cash_sources()])
    report = FinanceOps(w.svc).report(w.viewer, w.company, date(2026, 9, 1), date(2026, 9, 3))
    assert report['period'] == {'start': '2026-09-01', 'end': '2026-09-03'}
    assert report['official_tax_ready'] is False and report['company_data_complete'] is False
    with pytest.raises(DomainError) as failure:
        FinanceOps(w.svc).report(w.other_owner, w.company, date(2026, 9, 1), date(2026, 9, 3))
    assert failure.value.status == 404


def test_native_revision_is_producer_owned_and_stale_request_is_rejected(w):
    coverage(w, [PO, INVOICE, cash_sources()])
    with TestClient(create_app(w.db.path)) as client:
        login = client.post('/api/login', json={'username': w.viewer.username, 'password': PASSWORD})
        assert login.status_code == 200
        path = '/api/companies/' + w.company + '/owner/reports'
        first = client.get(path, params={'start': '2026-09-01', 'end': '2026-09-03'})
        assert first.status_code == 200
        revision = first.json()['source_revision']
        assert revision.startswith('sha256:') and len(revision) == 71
        repeated = client.get(path, params={
            'start': '2026-09-01', 'end': '2026-09-03', 'expected_source_revision': revision,
        })
        assert repeated.status_code == 200 and repeated.json()['source_revision'] == revision
        stale = client.get(path, params={
            'start': '2026-09-01', 'end': '2026-09-03', 'expected_source_revision': 'sha256:' + '0' * 64,
        })
        assert stale.status_code == 409 and stale.json()['code'] == 'REPORT_SOURCE_CHANGED'


def test_native_read_never_silently_discards_opaque_ids_or_revision(w):
    with TestClient(create_app(w.db.path)) as client:
        assert client.post('/api/login', json={'username': w.viewer.username, 'password': PASSWORD}).status_code == 200
        path = '/api/companies/' + w.company + '/owner/reports'
        for extra in ({'report_id': 'synthetic-report'}, {'source_revision': 'caller-string'}, {'target': 'https://unregistered.invalid'}):
            response = client.get(path, params={'start': '2026-09-01', 'end': '2026-09-30', **extra})
            assert response.status_code == 422


def test_ledger_and_source_report_share_one_snapshot_when_writer_changes_between_reads(w, monkeypatch):
    work, _ = coverage(w, [PO, INVOICE, cash_sources()])
    w.post(w.tx())
    start_date, end_date = date(2026, 9, 1), date(2026, 9, 30)
    baseline = FinanceOps(w.svc).report(w.owner, w.company, start_date, end_date)
    bank = next(event for event in work.list(w.owner, w.company) if event['kind'] == 'bank')
    document = Ingestion(w.svc).uploads.document(w.owner, w.company, bank['document_ids'][0])
    original_list = w.db.list
    changed = False

    def change_after_journal_read(connection, company, kind):
        nonlocal changed
        rows = original_list(connection, company, kind)
        if kind == 'journal' and company == w.company and not changed:
            changed = True
            # Real WAL commits, in this exact order: no sleeps or mocked stores.
            w.post(w.tx())
            Extractions(w.svc).reprocess(w.owner, w.company, document['id'], ReprocessInput(
                expected_revision=document['revision'], idempotency_key='native-snapshot-source-change',
                reason='isolated source revision after ledger commit',
            ))
        return rows

    monkeypatch.setattr(w.db, 'list', change_after_journal_read)
    observed = FinanceOps(w.svc).report(w.owner, w.company, start_date, end_date)
    assert changed
    assert observed['profit'] == baseline['profit']
    assert observed['payable_coverage'] == baseline['payable_coverage']
    assert observed['source_revision'] == baseline['source_revision']
    monkeypatch.undo()
    current = FinanceOps(w.svc).report(w.owner, w.company, start_date, end_date)
    profit = current.get('profit')
    assert isinstance(profit, dict) and profit.get('revenue') == 2000
    assert current['source_revision'] != baseline['source_revision']
