"""Compare only the rows a Notion plan can load.

Party and commitment rows are child records of a plan target. They are not
added accounts, projects, activities, or finance rows. Holds stay out of the
company class denominator and are reported beside the compare result.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path

PLAN_TARGET_KINDS = frozenset({"account", "engagement", "activity", "financial_item"})


def plan_target_inserts(insert_kinds: Mapping[str, int]) -> int:
    return sum(count for kind, count in insert_kinds.items() if kind in PLAN_TARGET_KINDS)


def added_count_matches(plan_added: int, insert_kinds: Mapping[str, int]) -> bool:
    return plan_target_inserts(insert_kinds) == plan_added


def compare_loaded_company(
    db_path: Path,
    *,
    loaded_by_class: Mapping[str, int],
    loadable: int,
    purchase_gross: int,
    hold_count: int,
) -> dict[str, object]:
    uri = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        rows = con.execute(
            """
            SELECT CASE json_extract(body,'$.direction')
              WHEN 'purchase' THEN '매입' WHEN 'sale' THEN '매출'
              WHEN 'receipt' THEN '기타입금' WHEN 'payment' THEN '기타출금'
              ELSE json_extract(body,'$.direction') END, count(*),
              coalesce(sum(json_extract(body,'$.gross')),0)
            FROM objects WHERE kind='transaction' GROUP BY 1
            """
        ).fetchall()
        journals = con.execute("SELECT count(*) FROM objects WHERE kind='journal'").fetchone()[0]
        natural_keys, unique_keys = con.execute(
            "SELECT count(*), count(DISTINCT external_id) FROM external_ids WHERE namespace='transaction:notion'"
        ).fetchone()
    finally:
        con.close()
    db_by_class = {name: count for name, count, _gross in rows}
    db_purchase_gross = next((int(gross) for name, _count, gross in rows if name == "매입"), 0)
    expected = {name: count for name, count in loaded_by_class.items() if count}
    class_ok = db_by_class == expected
    keys_ok = natural_keys == unique_keys == loadable
    gross_ok = db_purchase_gross == purchase_gross
    ok = integrity == "ok" and journals == 0 and keys_ok and class_ok and gross_ok
    return {
        "ok": bool(ok),
        "holdCount": hold_count,
        "integrity": integrity,
        "journals": journals,
        "naturalKeys": natural_keys,
        "uniqueNaturalKeys": unique_keys,
        "loadable": loadable,
        "classMatch": class_ok,
        "keyMatch": keys_ok,
        "purchaseGrossMatch": gross_ok,
    }
