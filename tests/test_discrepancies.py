from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from price.api import create_app
from price.db import DomainError
from price.discrepancies import Discrepancies, freshness_of, krw, revision_text
from price.review_cases import ReviewCases, ReviewRequest
from tests.conftest import PASSWORD
from tests.test_casework import answer, invoice, setup_sources
from tests.test_notion_finance_ingest import _apply, _row


def _rows(c):
    return c.execute("SELECT * FROM objects ORDER BY kind,id,revision").fetchall()


def test_missing_revision_is_unknown_and_invalid_money_is_not_zero():
    assert revision_text("  ") is None
    assert freshness_of(None, "2026-10-01T00:00:00Z") == "UNKNOWN"
    assert freshness_of("2026-10-01T00:00:00Z", None) == "UNKNOWN"
    assert freshness_of(revision_text("  "), "2026-10-01T00:00:00Z") == "UNKNOWN"
    assert freshness_of("2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z") == "STALE"
    assert freshness_of("2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z") == "STORED_UNCHANGED"
    assert krw(None) is None and krw(True) is None and krw("1100") is None and krw(1.2) is None


def test_same_page_revision_change_is_stale_without_writes(w):
    approval = "202601151234567890123456"
    _apply(w.db, w.owner, w.company, [_row("page-c", gross=1100, approval=approval)])
    _apply(w.db, w.owner, w.company, [_row("page-c", gross=2200, approval=approval, edited="2026-10-01T02:00:00.000Z")])
    with w.db.connect() as c:
        before = _rows(c)
    item = Discrepancies(w.svc).list(w.viewer, w.company)["items"][0]
    assert item["freshness"] == "STALE"
    assert item["resolved"] is False
    assert item["saved_source_revision"] == "2026-09-30T00:00:00.000Z"
    assert item["source_revision"] == "2026-10-01T02:00:00.000Z"
    assert item["differences"] == {"net": "1000", "tax": "100", "gross": "1100"}
    amounts = item["amounts"]
    assert amounts is not None and amounts["saved"] is not None
    assert amounts["saved"]["gross"] == "1100"
    assert amounts["incoming"]["gross"] == "2200"
    assert "same_page_saved_record_not_conflict_pair" in item["limitations"]
    assert approval not in json.dumps(item)
    with w.db.connect() as c:
        assert _rows(c) == before


def test_matching_stored_revision_is_unchanged_until_saved_marker_moves(w):
    edited = "2026-09-30T00:00:00.000Z"
    _apply(w.db, w.owner, w.company, [_row("page-z", gross=1000, classification="기타출금", edited=edited)])
    with w.db.transaction() as c:
        saved = c.execute(
            "SELECT id FROM objects WHERE company_id=? AND kind='transaction'",
            (w.company,),
        ).fetchone()
        current = w.db.get(c, w.company, "transaction", saved["id"])
        w.db.create(c, w.company, "notion_quarantine", {
            "page_id": "page-z",
            "reason": "synthetic same revision",
            "net": current["net"],
            "tax": current["tax"],
            "gross": current["gross"],
            "last_edited_time": edited,
            "author": w.owner.id,
        })
    box = Discrepancies(w.svc)
    item = box.list(w.owner, w.company)["items"][0]
    assert item["freshness"] == "STORED_UNCHANGED"
    assert item["resolved"] is False
    assert item["differences"] == {"net": "0", "tax": "0", "gross": "0"}
    assert "zero_delta_not_resolved" in item["limitations"]
    with w.db.transaction() as c:
        current = w.db.get(c, w.company, "transaction", item["saved_id"])
        w.db.replace(
            c, w.company, "transaction", item["saved_id"],
            {**current, "notion_provenance": {**current["notion_provenance"], "last_edited_time": "2026-11-01T00:00:00.000Z"}},
            current["revision"],
        )
    with w.db.connect() as c:
        before = _rows(c)
    stale = box.list(w.owner, w.company)["items"][0]
    assert stale["freshness"] == "STALE"
    assert stale["resolved"] is False
    assert stale["saved_source_revision"] == "2026-11-01T00:00:00.000Z"
    with w.db.connect() as c:
        assert _rows(c) == before


def test_unlinked_page_does_not_join_another_page(w):
    approval = "202601151234567890123456"
    _apply(w.db, w.owner, w.company, [_row("page-d", gross=1100, approval=approval)])
    _apply(w.db, w.owner, w.company, [_row("page-d", gross=1100, approval=approval), _row("page-e", gross=1100, approval=approval)])
    with w.db.connect() as c:
        owner = c.execute(
            "SELECT id FROM objects WHERE company_id=? AND kind='transaction'",
            (w.company,),
        ).fetchone()["id"]
    item = Discrepancies(w.svc).list(w.owner, w.company)["items"][0]
    assert item["page_id"] == "page-e"
    assert item["freshness"] == "UNKNOWN"
    assert item["saved_id"] is None
    assert item["differences"] is None
    assert item["saved_id"] != owner
    assert approval not in json.dumps(item)
    assert "saved_source_missing" in item["limitations"]


def test_review_source_supersede_is_stale_without_refresh_writes(w):
    work = setup_sources(w)
    event = invoice(work, w)
    review = ReviewCases(w.svc).open(w.owner, w.company, ReviewRequest(
        event_id=event["id"], reason="Source revision test", idempotency_key="f1-source-revision"))
    opened = next(row for row in Discrepancies(w.svc).list(w.owner, w.company)["items"] if row["id"] == review["id"])
    assert opened["freshness"] == "CURRENT"
    assert opened["professional_credential_verified"] is False
    assert opened["resolved"] is False
    with w.db.transaction() as c:
        source = w.db.get(c, w.company, "source_record", event["source_record_ids"][0])
        w.db.replace(c, w.company, "source_record", source["id"], {**source, "lifecycle": "SUPERSEDED"}, source["revision"])
    with w.db.connect() as c:
        before = _rows(c)
    stale = next(row for row in Discrepancies(w.svc).list(w.owner, w.company)["items"] if row["id"] == review["id"])
    assert stale["freshness"] == "STALE"
    with w.db.connect() as c:
        assert _rows(c) == before


def test_review_fact_change_is_stale(w):
    work = setup_sources(w)
    event = invoice(work, w)
    ReviewCases(w.svc).open(w.owner, w.company, ReviewRequest(
        event_id=event["id"], reason="Fact revision test", idempotency_key="f1-fact-revision"))
    answer(work, w, event, "business_use", "business")
    item = Discrepancies(w.svc).list(w.owner, w.company)["items"][0]
    assert item["kind"] == "expert_review"
    assert item["freshness"] == "STALE"
    assert item["resolved"] is False


def test_other_company_same_page_does_not_become_saved_revision(w):
    page = "shared-page"
    with w.db.transaction() as c:
        w.db.create(c, w.company, "notion_quarantine", {
            "page_id": page,
            "reason": "synthetic isolation",
            "net": 2000,
            "tax": 200,
            "gross": 2200,
            "last_edited_time": "2026-10-01T00:00:00.000Z",
            "author": w.owner.id,
        })
        other = w.db.create(c, w.other_company, "transaction", {
            "net": 9000,
            "tax": 999,
            "gross": 9999,
            "notion_provenance": {"page_id": page, "last_edited_time": "2026-12-01T00:00:00.000Z"},
            "author": w.other_owner.id,
        })
        c.execute(
            "INSERT INTO external_ids VALUES(?,?,?,?,?)",
            (w.other_company, "transaction:notion", "notion-page:" + page, "b" * 64, other["id"]),
        )
    item = Discrepancies(w.svc).list(w.owner, w.company)["items"][0]
    rendered = json.dumps(item)
    assert item["freshness"] == "UNKNOWN"
    assert item["saved_id"] is None
    assert item["differences"] is None
    assert item["saved_source_revision"] is None
    assert "saved_source_missing" in item["limitations"]
    assert other["id"] not in rendered
    assert "9999" not in rendered
    assert "2026-12-01" not in rendered
    assert Discrepancies(w.svc).list(w.other_owner, w.other_company)["items"] == []


def test_equal_source_marker_does_not_invalidate_stored_review(w):
    work = setup_sources(w)
    event = invoice(work, w)
    review = ReviewCases(w.svc).open(w.owner, w.company, ReviewRequest(
        event_id=event["id"], reason="Equal marker", idempotency_key="equal-source-marker"))
    with w.db.connect() as c:
        before = _rows(c)
    result = Discrepancies(w.svc).invalidate_changed_source(w.owner, w.company, review["id"])
    assert result["invalidated"] is False
    assert result["state"] == "UNASSIGNED"
    assert result["revision"] == review["revision"]
    with w.db.connect() as c:
        stored = w.db.get(c, w.company, "expert_review", review["id"])
        assert stored["state"] == "UNASSIGNED"
        assert _rows(c) == before


def test_changed_source_invalidates_stored_review_without_cross_tenant_write(w):
    work = setup_sources(w)
    event = invoice(work, w)
    review = ReviewCases(w.svc).open(w.owner, w.company, ReviewRequest(
        event_id=event["id"], reason="Changed source", idempotency_key="changed-source-marker"))
    with w.db.transaction() as c:
        current = w.db.get(c, w.company, "expert_review", review["id"])
        w.db.replace(c, w.company, "expert_review", review["id"], {**current, "state": "VERIFIED_FINDING"}, current["revision"])
        source = w.db.get(c, w.company, "source_record", event["source_record_ids"][0])
        w.db.replace(c, w.company, "source_record", source["id"], {**source, "lifecycle": "SUPERSEDED"}, source["revision"])
    with w.db.connect() as c:
        other_before = c.execute("SELECT * FROM objects WHERE company_id=? ORDER BY kind,id,revision", (w.other_company,)).fetchall()
        journals = c.execute("SELECT count(*) FROM objects WHERE company_id=? AND kind='journal'", (w.company,)).fetchone()[0]
    with pytest.raises(DomainError) as foreign:
        Discrepancies(w.svc).invalidate_changed_source(w.other_owner, w.company, review["id"])
    assert foreign.value.status == 404
    with pytest.raises(DomainError) as viewer:
        Discrepancies(w.svc).invalidate_changed_source(w.viewer, w.company, review["id"])
    assert viewer.value.status == 403
    result = Discrepancies(w.svc).invalidate_changed_source(w.owner, w.company, review["id"])
    assert result["invalidated"] is True
    assert result["state"] == "SOURCE_INVALIDATED"
    assert result["revision"] == review["revision"] + 2
    with w.db.connect() as c:
        stored = w.db.get(c, w.company, "expert_review", review["id"])
        assert stored["state"] == "SOURCE_INVALIDATED"
        assert stored["source_invalidated"] is True
        archived = c.execute(
            "SELECT body FROM object_history WHERE company_id=? AND kind='expert_review' AND id=? ORDER BY revision",
            (w.company, review["id"]),
        ).fetchall()
        assert "VERIFIED_FINDING" in archived[-1]["body"]
        assert c.execute("SELECT * FROM objects WHERE company_id=? ORDER BY kind,id,revision", (w.other_company,)).fetchall() == other_before
        assert c.execute("SELECT count(*) FROM objects WHERE company_id=? AND kind='journal'", (w.company,)).fetchone()[0] == journals
        before_read = _rows(c)
    item = next(row for row in Discrepancies(w.svc).list(w.owner, w.company)["items"] if row["id"] == review["id"])
    assert item["state"] == "SOURCE_INVALIDATED"
    assert item["freshness"] == "STALE"
    assert item["resolved"] is False
    with w.db.connect() as c:
        assert _rows(c) == before_read


def test_inbox_http_auth_company_and_method_boundaries(w):
    with TestClient(create_app(w.db.path)) as client:
        path = "/api/companies/" + w.company + "/discrepancies"
        assert client.get(path).status_code == 401
        assert client.post("/api/login", json={"username": w.viewer.username, "password": PASSWORD}).status_code == 200
        body = client.get(path).json()
        assert body == {"items": [], "read_only": True, "live_source_checked": False}
        assert client.get("/api/companies/" + w.other_company + "/discrepancies").status_code == 404
        assert client.post(path, json={}).status_code == 405
    with pytest.raises(DomainError):
        Discrepancies(w.svc).list(w.other_owner, w.company)
