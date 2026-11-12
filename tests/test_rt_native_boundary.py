"""Local native RT preparation; no accepted CRM mapping or transport grant."""
import hashlib
import json

from fastapi.testclient import TestClient

from price.api import create_app
from tests.conftest import PASSWORD
from tests.test_casework import setup_sources
from tests.test_payable_coverage import (
    INVOICE,
    PO,
    cash_sources,
    unclassified_cash_source,
)


def test_native_rt_reconciliation_fences_without_invented_crm_mapping(w):
    # Retained original-source fixtures, not manual transactions or CRM DB writes.
    documents = [PO, INVOICE, cash_sources(), unclassified_cash_source()]
    setup_sources(w, documents=documents)
    base = '/api/companies/' + w.company
    period = {'start': '2026-09-01', 'end': '2026-09-05'}
    statuses = []
    with TestClient(create_app(w.db.path)) as client:
        login = client.post('/api/login', json={'username': w.owner.username, 'password': PASSWORD})
        assert login.status_code == 200
        client.headers['x-csrf-token'] = login.json()['csrf']
        before = client.get(base + '/owner/reports', params=period)
        assert before.status_code == 200
        old_revision = before.json()['source_revision']
        reconciled = client.post(base + '/event-settlements/reconcile', json={})
        assert reconciled.status_code == 200 and reconciled.json()['new_links'] == 1
        current = client.get(base + '/owner/reports', params=period)
        assert current.status_code == 200
        report = current.json()
        revision = report['source_revision']
        assert revision != old_revision
        row, = report['payable_coverage']['purchase_obligations']
        assert row['project_id'] == 'SYNTHETIC-PROJECT-01'
        assert row['order_reference'] == 'SYNTHETIC-PO-01'
        assert row['invoice_allocated'] == 300 and row['net_linked_cash'] == 450
        assert row['unresolved_cash_outflows'] == 125 and row['order_cash_residual'] is None
        assert row['payable_today'] is None and row['confirmed'] is False
        assert report['official_tax_ready'] is False and report['company_data_complete'] is False
        stale = client.get(base + '/owner/reports', params={**period, 'expected_source_revision': old_revision})
        assert stale.status_code == 409 and stale.json()['code'] == 'REPORT_SOURCE_CHANGED'
        replay = client.post(base + '/event-settlements/reconcile', json={})
        assert replay.status_code == 200 and replay.json()['new_links'] == 0
        pinned = client.get(base + '/owner/reports', params={**period, 'expected_source_revision': revision})
        assert pinned.status_code == 200 and pinned.json()['source_revision'] == revision
        missing_dates = client.get(base + '/owner/reports')
        assert missing_dates.status_code == 422
        # No opaque archive->company/project/date mapping exists on this route.
        unmapped = client.get(base + '/owner/reports', params={
            **period, 'crm_workspace_id': 'SYNTHETIC-CRM-WORKSPACE',
            'crm_reference_id': 'SYNTHETIC-UNMAPPED-REFERENCE', 'crm_plan_version': 1,
        })
        assert unmapped.status_code == 422
        originals = set()
        for ident in row['document_ids']:
            document = client.get(base + '/documents/' + ident)
            assert document.status_code == 200
            originals.add(document.json()['sha256'])
        assert originals == {hashlib.sha256(raw).hexdigest() for raw in documents}
        assert client.get(base + '/journals').json() == []
        after_denials = client.get(base + '/owner/reports', params={**period, 'expected_source_revision': revision})
        assert after_denials.status_code == 200 and after_denials.json()['source_revision'] == revision
        statuses = [before.status_code, reconciled.status_code, current.status_code,
                    stale.status_code, replay.status_code, pinned.status_code,
                    missing_dates.status_code, unmapped.status_code, after_denials.status_code]
    with TestClient(create_app(w.db.path)) as viewer:
        login = viewer.post('/api/login', json={'username': w.viewer.username, 'password': PASSWORD})
        assert login.status_code == 200
        viewer.headers['x-csrf-token'] = login.json()['csrf']
        denied = viewer.post(base + '/event-settlements/reconcile', json={})
        assert denied.status_code == 403
        unchanged = viewer.get(base + '/owner/reports', params={**period, 'expected_source_revision': revision})
        assert unchanged.status_code == 200
    with TestClient(create_app(w.db.path)) as foreign:
        login = foreign.post('/api/login', json={'username': w.other_owner.username, 'password': PASSWORD})
        assert login.status_code == 200
        foreign.headers['x-csrf-token'] = login.json()['csrf']
        denied_foreign = foreign.post(base + '/event-settlements/reconcile', json={})
        assert denied_foreign.status_code == 404
    print(json.dumps({
        'proof_level': 'RT_NATIVE_PREPARATION_LOCAL_ONLY',
        'company_id': w.company, 'period': period,
        'before_source_revision': old_revision, 'after_source_revision': revision,
        'po_event_id': row['po_event_id'], 'source_project_id': row['project_id'],
        'invoice_event_ids': row['invoice_event_ids'],
        'unresolved_cash_event_ids': row['unresolved_cash_event_ids'],
        'native_statuses': statuses, 'viewer_command': 403, 'foreign_command': 404,
        'original_digests': sorted(originals), 'journal_effects': 0,
        'crm_mapping': 'NOT_REGISTERED', 'transport_accepted': False,
    }, sort_keys=True))
