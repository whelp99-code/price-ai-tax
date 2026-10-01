"""Synthetic draft ingest. No live snapshot and no demo database."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from price.auth import Actor
from price.db import Database
from price.models import CompanyInput
from price.notion_finance_ingest import apply_drafts, edited_index
from price.notion_finance_map import map_snapshot
from price.service import Service


def _row(page, gross=1100, approval="202601151234567890123456", classification="매입", edited="2026-09-30T00:00:00.000Z"):
    net = gross - gross // 11
    tax = gross - net
    if classification in ("기타입금", "기타출금"):
        net, tax = gross, 0
    return {
        "sourceId": page,
        "lastEditedTime": edited,
        "properties": {
            "구분": classification,
            "금액": net,
            "부가세": tax,
            "합계": gross,
            "거래명": f"합성 | 품목 | 2026-01-15",
            "자연키": f"invoice|{approval}" if classification in ("매입", "매출") else f"bank|{page}",
            "메모": f"홈택스 승인번호: {approval}" if classification in ("매입", "매출") else "",
            "실제 처리일": "2026-01-15",
        },
    }


@pytest.fixture
def scratch(tmp_path: Path):
    db = Database(tmp_path / "scratch.sqlite3")
    svc = Service(db)
    company, actor = svc.auth.bootstrap(CompanyInput(name="scratch-s3"), "scratch-s3", "scratch-secret-s3")
    return db, svc, company, actor


def _apply(db, actor, company, rows, external=None):
    report = map_snapshot(rows, external or {})
    return apply_drafts(db, actor, company, report, edited_index(rows))


def test_insert_is_idempotent_and_draft_only(scratch):
    db, svc, company, actor = scratch
    row = _row("page-a")
    first = _apply(db, actor, company, [row])
    second = _apply(db, actor, company, [row])
    assert first["inserted"] == 1 and first["journals"] == 0
    assert second["inserted"] == 0 and second["updated"] == 0 and second["unchanged"] == 1
    assert second["quarantined"] == 0
    with db.connect() as c:
        body = json.loads(c.execute("SELECT body FROM objects WHERE kind='transaction'").fetchone()["body"])
        kinds = [r[0] for r in c.execute("SELECT DISTINCT kind FROM objects")]
    assert body["notion_provenance"] == {"page_id": "page-a", "last_edited_time": "2026-09-30T00:00:00.000Z"}
    assert "journal" not in kinds


def test_bank_amount_change_updates_draft(scratch):
    db, svc, company, actor = scratch
    _apply(db, actor, company, [_row("page-b", gross=5000, classification="기타출금")])
    changed = _apply(db, actor, company, [_row("page-b", gross=7000, classification="기타출금", edited="2026-10-01T01:00:00.000Z")])
    assert changed["updated"] == 1 and changed["inserted"] == 0 and changed["journals"] == 0
    with db.connect() as c:
        gross = c.execute("SELECT json_extract(body,'$.gross') FROM objects WHERE kind='transaction'").fetchone()[0]
    assert int(gross) == 7000


def test_approval_amount_mismatch_is_quarantined(scratch):
    db, svc, company, actor = scratch
    _apply(db, actor, company, [_row("page-c", gross=1100)])
    conflict = _row("page-c", gross=2200, edited="2026-10-01T02:00:00.000Z")
    result = _apply(db, actor, company, [conflict])
    assert result["quarantined"] == 1 and result["updated"] == 0 and result["inserted"] == 0
    with db.connect() as c:
        gross = c.execute("SELECT json_extract(body,'$.gross') FROM objects WHERE kind='transaction'").fetchone()[0]
        q = c.execute("SELECT count(*) FROM objects WHERE kind='notion_quarantine'").fetchone()[0]
    assert int(gross) == 1100 and q == 1
    again = _apply(db, actor, company, [conflict])
    assert again["quarantined"] == 1 and again["updated"] == 0
    with db.connect() as c:
        gross = c.execute("SELECT json_extract(body,'$.gross') FROM objects WHERE kind='transaction'").fetchone()[0]
    assert int(gross) == 1100


def test_other_page_cannot_take_existing_approval(scratch):
    db, svc, company, actor = scratch
    _apply(db, actor, company, [_row("page-d", gross=1100)])
    stolen = _apply(db, actor, company, [_row("page-d", gross=1100), _row("page-e", gross=1100)])
    assert stolen["quarantined"] == 1 and stolen["inserted"] == 0
    with db.connect() as c:
        n = c.execute("SELECT count(*) FROM objects WHERE kind='transaction'").fetchone()[0]
    assert n == 1


def test_posted_journal_blocks_update(scratch):
    db, svc, company, actor = scratch
    _apply(db, actor, company, [_row("page-f", gross=5000, classification="기타출금")])
    with db.connect() as c:
        tx = c.execute("SELECT id FROM objects WHERE kind='transaction'").fetchone()["id"]
    with db.transaction() as c:
        c.execute(
            "INSERT INTO objects VALUES(?,?,?,?,?,?,?)",
            (
                company,
                "journal",
                "journal_test",
                1,
                json.dumps({"transaction_id": tx, "state": "POSTED", "event_date": "2026-01-15"}),
                "2026-01-15T00:00:00+00:00",
                "2026-01-15T00:00:00+00:00",
            ),
        )
    result = _apply(db, actor, company, [_row("page-f", gross=9000, classification="기타출금")])
    assert result["updated"] == 0 and result["quarantined"] == 1
    with db.connect() as c:
        gross = c.execute("SELECT json_extract(body,'$.gross') FROM objects WHERE kind='transaction'").fetchone()[0]
        posted = c.execute("SELECT json_extract(body,'$.state') FROM objects WHERE kind='journal'").fetchone()[0]
    assert int(gross) == 5000 and posted == "POSTED"


def test_disappeared_page_is_reported_not_deleted(scratch):
    db, svc, company, actor = scratch
    _apply(db, actor, company, [_row("page-g", classification="기타입금", gross=1000)])
    result = _apply(db, actor, company, [])
    assert result["disappeared"] == ["page-g"]
    with db.connect() as c:
        n = c.execute("SELECT count(*) FROM objects WHERE kind='transaction'").fetchone()[0]
    assert n == 1
