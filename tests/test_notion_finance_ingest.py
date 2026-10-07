"""Synthetic draft ingest. No live snapshot and no demo database."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from price.auth import Actor
from price.db import Database, DomainError, canonical, now, uid
from price.models import CompanyInput, UserInput
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


def test_unchanged_reingestion_performs_no_write(scratch):
    db, svc, company, actor = scratch
    row = _row("page-replay")
    assert _apply(db, actor, company, [row])["inserted"] == 1
    with db.transaction() as c:
        c.execute("CREATE TABLE write_probe(n INTEGER)")
        for table in ("objects", "external_ids", "object_history", "audit"):
            c.execute(
                f"CREATE TRIGGER probe_{table}_ins AFTER INSERT ON {table} "
                "BEGIN INSERT INTO write_probe VALUES(1); END"
            )
            c.execute(
                f"CREATE TRIGGER probe_{table}_upd AFTER UPDATE ON {table} "
                "BEGIN INSERT INTO write_probe VALUES(1); END"
            )
    second = _apply(db, actor, company, [row])
    with db.connect() as c:
        writes = c.execute("SELECT count(*) FROM write_probe").fetchone()[0]
    assert second["inserted"] == 0 and second["updated"] == 0 and second["unchanged"] == 1
    assert second["provenance"] == 0 and second["quarantined"] == 0 and second["journals"] == 0
    assert writes == 0


def test_viewer_cannot_mutate_existing_draft(scratch):
    db, svc, company, actor = scratch
    _apply(db, actor, company, [_row("page-viewer")])
    viewer = svc.add_user(
        actor,
        company,
        UserInput(username="scratch-viewer", password="Test-Only-Price-2026!", role="viewer"),
    )
    viewer_actor = Actor(viewer["id"], viewer["username"])

    def persisted_rows():
        with db.connect() as c:
            return tuple(
                tuple(c.execute(f"SELECT * FROM {table} ORDER BY 1,2,3").fetchall())
                for table in ("objects", "external_ids", "object_history", "audit")
            )

    before = persisted_rows()
    report = map_snapshot([_row("page-viewer", gross=2200)], {})
    with pytest.raises(DomainError) as error:
        apply_drafts(db, viewer_actor, company, report, {"page-viewer": "2026-10-01T00:00:00Z"})
    assert getattr(error.value, "code", None) == "FORBIDDEN"
    assert persisted_rows() == before


def test_viewer_cannot_apply_empty_ingest_report(scratch):
    db, svc, company, actor = scratch
    viewer = svc.add_user(
        actor,
        company,
        UserInput(username="empty-viewer", password="Test-Only-Price-2026!", role="viewer"),
    )
    viewer_actor = Actor(viewer["id"], viewer["username"])
    report = map_snapshot([], {})
    with db.connect() as c:
        before = {
            table: tuple(c.execute(f"SELECT * FROM {table} ORDER BY 1,2,3").fetchall())
            for table in ("objects", "external_ids", "object_history", "audit")
        }
    with pytest.raises(DomainError) as error:
        apply_drafts(db, viewer_actor, company, report, edited_index([]))
    assert error.value.code == "FORBIDDEN"
    with db.connect() as c:
        after = {
            table: tuple(c.execute(f"SELECT * FROM {table} ORDER BY 1,2,3").fetchall())
            for table in ("objects", "external_ids", "object_history", "audit")
        }
    assert after == before


def test_foreign_company_actor_cannot_mutate_existing_draft(scratch):
    db, svc, company, actor = scratch
    _apply(db, actor, company, [_row("page-foreign")])
    other_company = uid("company")
    with db.transaction() as c:
        c.execute(
            "INSERT INTO companies VALUES(?,?,?)",
            (
                other_company,
                canonical({"name": "synthetic foreign", "demo": False, "ledger_mode": "validation"}),
                now(),
            ),
        )
        foreign = svc.auth.add_user(
            c,
            other_company,
            UserInput(username="foreign-owner", password="Test-Only-Price-2026!", role="owner"),
        )

    def persisted_rows():
        with db.connect() as c:
            return tuple(
                tuple(c.execute(f"SELECT * FROM {table} ORDER BY 1,2,3").fetchall())
                for table in ("objects", "external_ids", "object_history", "audit")
            )

    before = persisted_rows()
    report = map_snapshot([_row("page-foreign", gross=2200)], {})
    with pytest.raises(DomainError) as error:
        apply_drafts(
            db,
            Actor(foreign["id"], foreign["username"]),
            company,
            report,
            {"page-foreign": "2026-10-01T00:00:00Z"},
        )
    assert error.value.code == "NOT_FOUND"
    assert persisted_rows() == before


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
