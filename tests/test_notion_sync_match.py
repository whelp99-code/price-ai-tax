"""Plan-target scratch counts and hold-excluded company compare."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from price.notion_finance_map import map_snapshot
from price.notion_sync_match import added_count_matches, compare_loaded_company, plan_target_inserts


def _row(page: str, classification: str, tax: int | None, approval: str) -> dict:
    gross = 1100
    net = 1000
    return {
        "sourceId": page,
        "properties": {
            "구분": classification,
            "금액": net,
            "부가세": tax,
            "합계": gross if tax is not None else net,
            "거래명": "합성 | 2026-09-01",
            "자연키": f"invoice|{approval}",
            "메모": "",
            "실제 처리일": "2026-09-01",
        },
    }


def test_loaded_class_counts_omit_holds() -> None:
    report = map_snapshot([
        _row("page-loaded", "매출", 100, "202601151234567890123456"),
        _row("page-held", "매입", None, "202601151234567890123457"),
    ])
    assert report.by_class == {"매출": 1}
    assert report.hold_count == 1
    assert [item.page_id for item in report.holds] == ["page-held"]


def test_added_count_ignores_party_and_commitment() -> None:
    kinds = {"commitment": 17, "party": 4, "engagement": 1, "financial_item": 3}
    assert plan_target_inserts(kinds) == 4
    assert added_count_matches(4, kinds) is True
    assert added_count_matches(25, kinds) is False


def _company_db(path: Path, *, payment_extra: int = 0) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE objects (kind TEXT, body TEXT);
        CREATE TABLE external_ids (namespace TEXT, external_id TEXT);
        INSERT INTO objects VALUES ('transaction', '{"direction":"sale","gross":1}');
        INSERT INTO external_ids VALUES ('transaction:notion', 'page-loaded');
        """
    )
    for index in range(payment_extra):
        con.execute(
            "INSERT INTO objects VALUES ('transaction', ?)",
            ('{"direction":"payment","gross":1}',),
        )
        con.execute(
            "INSERT INTO external_ids VALUES ('transaction:notion', ?)",
            (f"extra-{index}",),
        )
    con.commit()
    con.close()


def test_holds_do_not_fail_company_compare(tmp_path: Path) -> None:
    db = tmp_path / "company.sqlite3"
    _company_db(db)
    result = compare_loaded_company(
        db,
        loaded_by_class={"매출": 1},
        loadable=1,
        purchase_gross=0,
        hold_count=3,
    )
    assert result["ok"] is True
    assert result["holdCount"] == 3


def test_extra_loaded_row_still_fails_and_keeps_hold_count(tmp_path: Path) -> None:
    db = tmp_path / "company.sqlite3"
    _company_db(db, payment_extra=1)
    result = compare_loaded_company(
        db,
        loaded_by_class={"매출": 1},
        loadable=1,
        purchase_gross=0,
        hold_count=3,
    )
    assert result["ok"] is False
    assert result["holdCount"] == 3
    assert result["classMatch"] is False
