"""Synthetic purchase issue-date window. No real snapshot or approval body."""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pytest

from price.notion_finance_map import MapReport, map_row
from tests.test_hometax_purchase_csv import HEADER, S2, _csv_row


INSIDE = "202601151234567890123456"
OUTSIDE = "202601019999999999999999"
HELD = "202602021111111111111111"
AMOUNT_ONLY = "202603033333333333333333"
UNRELATED = "202604044444444444444444"
START = date(2026, 9, 1)
END = date(2026, 10, 20)


def _purchase(page: str, approval: str, title: str, gross: int = 1100, tax: int = 100) -> dict:
    return {
        "sourceId": page,
        "properties": {
            "구분": "매입",
            "금액": gross - tax,
            "부가세": tax,
            "합계": gross,
            "거래명": title,
            "자연키": f"invoice|{approval}",
            "메모": f"홈택스 승인번호: {approval}",
            "실제 처리일": "2026-10-19",
            "createdAt": "2026-10-19T22:00:02Z",
        },
    }


def _report() -> MapReport:
    rows = [
        _purchase("page-in", INSIDE, "합성 | 2026-10-20"),
        _purchase("page-out", OUTSIDE, "합성 | 2026-08-31"),
        _purchase("page-held", HELD, "보류 | 2026-09-16", gross=110, tax=10),
        _purchase("page-edge", AMOUNT_ONLY, "경계 | 2026-09-01"),
    ]
    report = MapReport(rows=len(rows))
    for row in rows:
        item = map_row(row)
        report.by_class[item.classification] = report.by_class.get(item.classification, 0) + 1
        report.holds.append(item) if item.held else report.loaded.append(item)
    return report


def test_title_issue_date_ignores_processed_date_and_created_at():
    item = map_row(_purchase("page-in", INSIDE, "합성 | 2026-10-20"))
    assert item.issue_date == date(2026, 10, 20)
    assert item.draft.event_date == date(2026, 10, 19)
    assert item.issue_date != item.draft.event_date


def test_boundary_inside_and_outside_are_separate_rows():
    report = _report()
    period = S2.purchase_period_report(report, (START, END))
    assert period["in_count"] == 3
    assert period["out_count"] == 1
    assert period["out_gross"] == 1100
    assert period["missing_issue_date_count"] == 0
    assert period["blocked"] is False
    assert report.purchase_notion_gross() == period["in_gross"] + period["out_gross"]


def test_missing_or_unparseable_issue_date_blocks_pass():
    report = _report()
    report.holds.extend(report.loaded)
    report.loaded.clear()
    report.holds.append(map_row(_purchase("page-missing", UNRELATED, "작성일 없음")))
    period = S2.purchase_period_report(report, (START, END))
    assert period["missing_issue_date_count"] == 1
    assert period["blocked"] is True
    text = S2.reconciliation(report, {}, None, period)
    assert "| hometax target vs loaded |" in text
    assert "PASS" not in text.split("hold_explains")[0]
    assert "ISSUE_DATE_MISSING" in text
    assert "2026-10-19T22:00:02Z" not in text


def test_invalid_period_and_filename_conflicts_fail_closed(tmp_path: Path):
    (tmp_path / "hometax_purchase_etax_20260901_20261020.csv").write_text(HEADER, encoding="utf-8")
    assert S2.resolve_purchase_period(tmp_path) == (START, END)
    with pytest.raises(SystemExit, match="after end"):
        S2.resolve_purchase_period(tmp_path, date(2026, 10, 21), date(2026, 9, 1))
    with pytest.raises(SystemExit, match="disagrees"):
        S2.resolve_purchase_period(tmp_path, date(2026, 9, 2), END)
    (tmp_path / "hometax_purchase_20260801_20260831.csv").write_text(HEADER, encoding="utf-8")
    with pytest.raises(SystemExit, match="exactly one"):
        S2.resolve_purchase_period(tmp_path)
    with pytest.raises(SystemExit, match="calendar date"):
        S2.parse_period_arg("2026-02-31", "period end")


def test_generic_dated_name_is_not_a_query_window(tmp_path: Path):
    (tmp_path / "BuyHomeTax_20260901_20261020.csv").write_text(
        HEADER + _csv_row(INSIDE, "1100", "1000", "100"),
        encoding="utf-8",
    )
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert found[INSIDE] == (1000, 100, 1100)
    assert separate == []
    with pytest.raises(SystemExit, match="purchase period missing"):
        S2.resolve_purchase_period(tmp_path)


def test_zero_rows_still_reports_explicit_window():
    period = S2.purchase_period_report(MapReport(), (START, END))
    assert period["in_count"] == period["out_count"] == 0
    assert period["blocked"] is False


def test_partial_match_conflict_candidate_and_no_evidence_are_distinct():
    report = _report()
    report.holds.extend(item for item in report.loaded if item.approval == HELD)
    report.loaded = [item for item in report.loaded if item.approval != HELD]
    evidence = [
        {"approval": INSIDE, "net": 1000, "tax": 100, "gross": 1100, "issue_date": "2026-10-20"},
        {"approval": "202607077777777777777777", "net": 100, "tax": 10, "gross": 110, "issue_date": "2026-09-16"},
        {"approval": HELD, "net": 100, "tax": 10, "gross": 1, "issue_date": "2026-09-16"},
        {"approval": UNRELATED, "net": 1000, "tax": 100, "gross": 1100, "issue_date": "2026-09-16"},
        {"approval": "202605055555555555555555", "net": 9, "tax": 1, "gross": 10, "issue_date": "2026-09-03"},
    ]
    links = {item["approval_tail"]: item for item in S2.purchase_period_report(report, (START, END), evidence)["links"]}
    assert links["3456"]["state"] == "match"
    assert links["1111"]["state"] == "conflict"
    assert links["4444"]["state"] == "candidate"
    assert links["5555"]["state"] == "no-evidence"
    assert "not the same transaction" in links["4444"]["detail"]
    text = S2.reconciliation(report, {INSIDE: (1000, 100, 1100)}, None, S2.purchase_period_report(report, (START, END), evidence))
    assert "PERIOD_LINK match" in text
    assert "PERIOD_LINK conflict" in text
    assert "PERIOD_LINK candidate" in text
    assert "PERIOD_LINK no-evidence" in text
    assert UNRELATED not in text


def test_held_approval_still_blocks_pass_after_period_split():
    report = _report()
    report.holds.extend(report.loaded)
    report.loaded.clear()
    hometax = {INSIDE: (1000, 100, 1100)}
    period = S2.purchase_period_report(report, (START, END))
    text = S2.reconciliation(report, hometax, None, period)
    assert "| notion purchases outside period | 1 | gross 1100" in text
    assert "PASS" not in text.split("hold_explains")[0]


@pytest.mark.parametrize("explicit", [False, True])
def test_main_scopes_primary_verdict_with_or_without_flags(tmp_path, monkeypatch, capsys, explicit):
    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / "hometax_purchase_etax_20260901_20261020.csv").write_text(
        HEADER + _csv_row(INSIDE, "1100", "1000", "100"), encoding="utf-8"
    )
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"finance": [
        _purchase("page-in", INSIDE, "합성 | 2026-10-20"),
        _purchase("page-out", OUTSIDE, "합성 | 2026-08-31"),
    ]}), encoding="utf-8")
    argv = ["s2", "--snapshot", str(snapshot), "--hometax-dir", str(exports)]
    if explicit:
        argv += ["--period-start", "2026-09-01", "--period-end", "2026-10-20"]
    monkeypatch.setattr("sys.argv", argv)
    S2.main()
    text = capsys.readouterr().out
    assert "NOT_RUN; unscoped" not in text
    assert "purchase_notion_gross 1100\n" in text
    assert "| hometax target vs loaded | 1100 vs 1100 | PASS gap=0 |" in text
    assert "| notion purchases outside period | 1 | gross 1100" in text


def test_period_missing_date_blocks_primary_verdict_even_if_in_window_totals_match():
    report = MapReport()
    report.loaded = [
        map_row(_purchase("in", INSIDE, "합성 | 2026-09-01")),
        map_row(_purchase("missing", OUTSIDE, "작성일 없음")),
    ]
    period = S2.purchase_period_report(report, (START, END))
    text = S2.reconciliation(report, {INSIDE: (1000, 100, 1100)}, None, period)
    assert "| hometax target vs loaded | 1100 vs 1100 | FAIL gap=0 |" in text
    assert "| notion purchases missing issue date | 1 | FAIL; PASS blocked" in text


def test_primary_verdict_excludes_outside_holds_and_explains_all_row_gap():
    report = MapReport()
    report.loaded = [map_row(_purchase("in", INSIDE, "합성 | 2026-09-01"))]
    report.holds = [map_row(_purchase("out", OUTSIDE, "보류 | 2026-08-31", tax=0))]
    report.hometax_separate = [{"gross": "330", "reason": "non-numeric approval"}]
    period = S2.purchase_period_report(report, (START, END))
    text = S2.reconciliation(report, {INSIDE: (1000, 100, 1100)}, None, period)
    assert "| loadable purchase gross | 1100 |" in text
    assert "| hold purchase gross | 0 |" in text
    assert "| unmatched notion purchase approvals | 0 |" in text
    assert "| hometax target vs loaded | 1100 vs 1100 | FAIL gap=0 |" in text
    assert "1430 vs 1100 | gap=330; separate 330 + numeric-target-minus-notion 0" in text


def test_main_without_flags_rejects_missing_filename_period(tmp_path, monkeypatch):
    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / "BuyHomeTax.csv").write_text(
        HEADER + _csv_row(INSIDE, "1100", "1000", "100"), encoding="utf-8"
    )
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text('{"finance": []}', encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["s2", "--snapshot", str(snapshot), "--hometax-dir", str(exports)])
    with pytest.raises(SystemExit, match="purchase period missing"):
        S2.main()


def test_explicit_period_rejects_extra_purchase_files_before_merge(tmp_path: Path):
    named = tmp_path / "hometax_purchase_etax_20260901_20261020.csv"
    named.write_text(HEADER + _csv_row(INSIDE, "1100", "1000", "100"), encoding="utf-8")
    other = tmp_path / "hometax_purchase_other.csv"
    other.write_text(HEADER + _csv_row(OUTSIDE, "2200", "2000", "200"), encoding="utf-8")
    unrelated = tmp_path / "notes.csv"
    unrelated.write_text("memo\nnot a tax file\n", encoding="utf-8")
    window = (START, END)
    with pytest.raises(SystemExit, match="unverified purchase file"):
        S2.resolve_purchase_period(tmp_path, START, END)
    with pytest.raises(SystemExit, match="unverified purchase file"):
        S2.read_hometax_purchase_sources(tmp_path, window)
    other.unlink()
    from tests.test_hometax_purchase_csv import _xlsx

    _xlsx(tmp_path / "TAX_extra_BuyHomeTax.xlsx", [(OUTSIDE, "2000", "200", "2200")])
    with pytest.raises(SystemExit, match="unverified purchase file"):
        S2.resolve_purchase_period(tmp_path, START, END)
    with pytest.raises(SystemExit, match="unverified purchase file"):
        S2.read_hometax_purchase_sources(tmp_path, window)
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert found[INSIDE] == (1000, 100, 1100)
    assert found[OUTSIDE] == (2000, 200, 2200)
    assert separate == []
    assert unrelated.read_text(encoding="utf-8").startswith("memo")
