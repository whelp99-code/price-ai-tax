"""Non-24 Hometax approvals pair by supplier, date, and gross, or stay visible."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from price.notion_finance_map import map_snapshot


def _s2():
    path = Path(__file__).resolve().parents[1] / "scripts" / "notion_finance_s2.py"
    spec = importlib.util.spec_from_file_location("notion_finance_s2_aux", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


S2 = _s2()
DIGIT = "202610150000000000000001"
BIZ = "1234560121"


def _purchase(page: str, approval: str, gross: int, biz: str = "") -> dict:
    net, tax = 1000, 100
    if gross != 1100:
        net, tax = gross - 1, 1
    return {
        "sourceId": page,
        "properties": {
            "구분": "매입",
            "금액": net,
            "부가세": tax,
            "합계": gross,
            "거래명": f"synthetic | {page} | 2026-10-15",
            "자연키": f"invoice|{approval}" if approval else "invoice|short",
            "메모": f"사업자번호: {biz}" if biz else "",
            "실제 처리일": "2026-10-15",
        },
    }


def _separate(approval_tail: str, gross: str, biz: str, matched: bool) -> dict[str, str]:
    return {
        "file": "purchases.csv",
        "row": "2",
        "approval_tail": approval_tail,
        "gross": gross,
        "net": str(int(gross) - 1),
        "tax": "1",
        "supplier_biz": biz,
        "issue_date": "2026-10-15",
        "reason": "approval is not exactly 24 digits after hyphen removal; not coerced",
        "auxiliary_matched": "1" if matched else "0",
    }


def test_24_digit_pair_is_not_a_hometax_only_warning():
    report = map_snapshot([_purchase("page-24", DIGIT, 1100)])
    text = S2.reconciliation(report, {DIGIT: (1000, 100, 1100)}, None)
    assert "| hometax only warning | 0 | 홈택스에만 있음 |" in text
    assert "| hometax target vs loaded | 1100 vs 1100 | PASS gap=0 |" in text
    assert "홈택스에만 있음 approval…" not in text


def test_24_digit_without_pair_stays_unmatched_and_is_not_auxiliary():
    report = map_snapshot([_purchase("page-24", DIGIT, 1100, BIZ)])
    other = "202610150000000000000099"
    text = S2.reconciliation(report, {other: (1000, 100, 1100)}, None)
    assert "| unmatched notion purchase approvals | 1 |" in text
    assert "| hometax only warning | 0 | 홈택스에만 있음 |" in text
    assert "| hometax target vs loaded | 1100 vs 1100 | FAIL gap=0 |" in text


def test_non24_pair_by_biz_date_gross_is_not_a_warning_or_hold():
    report = map_snapshot([_purchase("page-aux", "", 384510, BIZ)])
    report.hometax_separate = [_separate("9795", "384510", BIZ, matched=False)]
    text = S2.reconciliation(report, {}, None)
    assert report.hometax_separate[0]["auxiliary_matched"] == "1"
    assert "| hometax only warning | 0 | 홈택스에만 있음 |" in text
    assert "홈택스에만 있음 approval…" not in text
    assert "HOLD approval…" not in text


def test_non24_without_pair_is_a_visible_warning_and_does_not_block_pass():
    report = map_snapshot([_purchase("page-24", DIGIT, 1100)])
    report.hometax_separate = [_separate("9795", "384510", BIZ, matched=False)]
    text = S2.reconciliation(report, {DIGIT: (1000, 100, 1100)}, None)
    assert report.hometax_separate[0]["auxiliary_matched"] == "0"
    assert "| hometax only warning | 1 | 홈택스에만 있음 |" in text
    assert "홈택스에만 있음 approval…9795" in text
    assert "HOLD approval…" not in text
    assert "| hometax target vs loaded | 1100 vs 1100 | PASS gap=0 |" in text
