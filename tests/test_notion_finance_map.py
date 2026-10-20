"""Synthetic fixtures only. Do not embed a real Notion snapshot."""
from datetime import date

import pytest

from pathlib import Path

from price.notion_finance_map import Hold, apply_external_amount_conflict, map_row, map_snapshot, purchase_match_verdict


def _row(**props):
    base = {
        "구분": "매입",
        "금액": 1000,
        "부가세": 100,
        "합계": 1100,
        "거래명": "합성 거래처 | 품목 | 2026-01-15",
        "자연키": "invoice|202601151234567890123456",
        "메모": "홈택스 승인번호: 202601151234567890123456",
        "실제 처리일": "2026-01-15",
    }
    base.update(props)
    return {"sourceId": props.pop("page", "page-synth-0001"), "properties": base}


def test_classification_and_keys():
    purchase = map_row(_row())
    assert purchase.kind == "invoice" and purchase.direction == "purchase"
    assert purchase.draft.tax_category == "standard"
    assert purchase.draft.deductible is None
    assert purchase.keys["notion-page"] == "page-synth-0001"
    assert purchase.keys["hometax-approval"] == "202601151234567890123456"
    assert purchase.draft.external_id == "notion-page:page-synth-0001"
    sale = map_row(_row(구분="매출", 자연키="userapproved|2026-02-01|sale", 메모="건별 파일 없음", page="page-sale"))
    assert sale.direction == "sale" and "hometax-approval" not in sale.keys
    receipt = map_row(_row(구분="기타입금", 금액=5000, 부가세=0, 합계=5000, 자연키="bank|x", 메모="", page="page-in"))
    payment = map_row(_row(구분="기타출금", 금액=5000, 부가세=0, 합계=5000, 자연키="bank|y", 메모="", page="page-out"))
    assert (receipt.kind, receipt.direction) == ("bank", "receipt")
    assert (payment.kind, payment.direction) == ("bank", "payment")
    assert receipt.draft.tax == 0 and receipt.draft.tax_category == "unknown"


def test_net_tax_gross_and_bank_tax_held_not_fixed():
    broken = map_row(_row(금액=3630000, 부가세=330000, 합계=3630000, page="page-inclusive"))
    assert broken.held
    assert broken.net == 3630000 and broken.gross == 3630000
    assert "not fixed" in broken.reason
    bank = map_row(_row(구분="기타출금", 금액=1000, 부가세=100, 합계=1100, page="page-bank-tax"))
    assert bank.held and "bank-only" in bank.reason
    assert bank.tax == 100


def test_nexias_style_external_conflict_is_hold_not_rewrite():
    """금액 equals the external gross (VAT-inclusive) and must not be rewritten."""
    page = "3bb33ddb-7425-8114-8769-d2a1e0c9bac2"
    approval = "202605221026052245410855"
    row = _row(
        page=page,
        금액=3300000,
        부가세=330000,
        합계=3630000,
        자연키=f"invoice|{approval}",
        메모=f"홈택스 승인번호: {approval}",
        거래명="합성 넥시아스 | license | 2026-05-22",
    )
    mapped = map_row(row)
    assert not mapped.held
    assert mapped.net == 3300000 and mapped.gross == 3630000
    external = {approval: (3000000, 300000, 3300000)}
    held = apply_external_amount_conflict(mapped, external)
    assert held.held
    assert held.net == 3300000 and held.tax == 330000 and held.gross == 3630000
    assert "not fixed" in held.reason
    report = map_snapshot([row], external)
    assert report.purchase_loaded_gross() == 0
    assert report.purchase_notion_gross() == 3630000
    assert report.holds[0].page_id == page


def test_matching_external_amounts_load():
    approval = "202601151234567890123456"
    row = _row()
    report = map_snapshot([row], {approval: (1000, 100, 1100)})
    assert len(report.loaded) == 1 and not report.holds


def test_approval_disagreement_and_missing_date():
    clash = map_row(_row(자연키="invoice|111111111111111111111111", 메모="홈택스 승인번호: 222222222222222222222222"))
    assert clash.held and "disagree" in clash.reason
    undated = map_row(_row(**{"실제 처리일": None, "거래명": "날짜 없음", "자연키": "invoice|202601151234567890123456", "메모": "승인만"}))
    assert undated.held and "date" in undated.reason


def test_zero_tax_invoice_not_guessed():
    held = map_row(_row(금액=1000, 부가세=0, 합계=1000))
    assert held.held and "not fixed" in held.reason


def test_event_date_from_processed_field():
    mapped = map_row(_row(**{"실제 처리일": "2026-08-13T18:58:00.000+09:00"}))
    assert mapped.draft.event_date == date(2026, 8, 13)


def test_unknown_classification():
    held = map_row(_row(구분="내부이체"))
    assert held.held


def test_verdict_fails_on_hardcoded_pass_string():
    hold = Hold(
        "3bb33ddb-7425-8114-8769-d2a1e0c9bac2",
        "매입",
        "amounts conflict",
        3300000,
        330000,
        3630000,
        approval_tail="0855",
        counterparty="주식회사 넥시아스",
        external_gross=3300000,
    )
    result = purchase_match_verdict(191_611_200, 194_911_200, [hold])
    blob = f"{result['status']} {result['hold_explains']}"
    assert result["status"] == "FAIL"
    assert result["gap"] == 3_300_000
    assert "PASS after hold resolved" not in blob
    assert "HOLD_EXPLAINS: 주식회사 넥시아스 …0855 held 3,630,000 vs Hometax 3,300,000 → gap closes if resolved" == result["hold_explains"]
    assert purchase_match_verdict(194_911_200, 194_911_200, [])["status"] == "PASS"
    script = Path(__file__).resolve().parents[1].joinpath("scripts/notion_finance_s2.py").read_text()
    assert "PASS after hold resolved" not in script
def test_labeled_approval_keeps_label_and_tail_is_not_merged():
    memo = "홈택스 승인번호: 20261015-41000021-b0009795"
    held = map_row(_row(자연키="bank|page-label", 메모=memo, page="page-label-tail"))
    assert held.held
    assert "not a complete 24-digit number" in held.reason
    assert held.approval_tail == "9795"
    assert "20261015-41000021-b0009795" not in held.reason


def test_missing_bank_tax_is_zero_not_invented_invoice_tax():
    bank = map_row(_row(구분="기타출금", 금액=100, 부가세=None, 합계=100, page="page-bank-empty-tax"))
    assert not bank.held and bank.tax == 0
    invoice = map_row(_row(부가세=None))
    assert invoice.held and "not fixed" in invoice.reason
