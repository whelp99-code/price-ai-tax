from datetime import date
from pathlib import Path

import pytest

from price.external_calc import korean_tax_calc as K
from price.vat_rules_kr import CommonInput

needs_engine = pytest.mark.skipif(K.installed_version() != K.PINNED_VERSION, reason="optional korean-tax-calc-mcp==0.12.2 is not installed")

EXAMPLES = [
    ("invoice_none", lambda: K.invoice_penalty(10_000_000, "미발급", date(2026, 3, 10)), 200_000),
    ("invoice_late", lambda: K.invoice_penalty(10_000_000, "지연발급", date(2026, 3, 10)), 100_000),
    ("late_payment_90d", lambda: K.late_payment_penalty(5_000_000, date(2026, 1, 25), date(2026, 4, 25)), 99_000),
    ("underreport_general", lambda: K.underreporting_penalty(3_000_000, False, tax_year=2026), 300_000),
    ("underreport_fraud", lambda: K.underreporting_penalty(3_000_000, True, tax_year=2026), 1_200_000),
    ("common_input", lambda: K.vat_common_input_allocation(2_000_000, 100_000_000, 30_000_000, period_end=date(2026, 6, 30)), 600_000),
    ("bad_debt", lambda: K.vat_bad_debt_credit(11_000_000, bad_debt_date=date(2026, 6, 30)), 1_000_000),
]
NO_INTERNAL_RULE = {"invoice_none", "invoice_late", "late_payment_90d", "underreport_general", "underreport_fraud", "bad_debt"}


def _refuse_engine(*_args, **_kwargs):
    raise AssertionError("external engine must not be called")


@pytest.mark.parametrize("name,call,expected", EXAMPLES, ids=[case[0] for case in EXAMPLES])
@needs_engine
def test_2026_examples_match_expected(name, call, expected):
    result = call()
    assert result["status"] == K.COMPUTED, result
    assert result["value"] == expected
    assert type(result["value"]) is int
    assert result["engine_version"] == K.PINNED_VERSION == "0.12.2"
    assert result["legal_correctness_verified"] is False
    assert result["requires_review"] is True
    assert result["authorities"]
    if name in NO_INTERNAL_RULE:
        assert result["internal_comparison"] == K.NOT_COMPARED
        assert result["internal_comparison_reason"] == "NO_VAT_RULES_KR_COUNTERPART"


@pytest.mark.parametrize("call,status", [
    (lambda: K.late_payment_penalty(5_000_000, date(2018, 1, 25), date(2018, 4, 25)), K.BLOCKED_KNOWN_ERROR),
    (lambda: K.late_payment_penalty(5_000_000, date(2019, 1, 25), date(2019, 4, 25)), K.BLOCKED_KNOWN_ERROR),
    (lambda: K.late_payment_penalty(5_000_000, date(2019, 2, 10), date(2019, 5, 11)), K.BLOCKED_KNOWN_ERROR),
    (lambda: K.invoice_penalty(10_000_000, "미발급", date(2027, 1, 10)), K.BLOCKED_UNSUPPORTED),
    (lambda: K.invoice_penalty(10_000_000, "미발급", date(2015, 3, 10)), K.BLOCKED_UNSUPPORTED),
    (lambda: K.underreporting_penalty(3_000_000, True, tax_year=2026, offshore=True), K.BLOCKED_UNSUPPORTED),
    (lambda: K.underreporting_penalty(3_000_000, False, tax_year=2026, amended_return_reduction=True), K.BLOCKED_UNSUPPORTED),
    (lambda: K.underreporting_penalty(3_000_000, False, tax_year=2026, offshore=True), K.BLOCKED_UNSUPPORTED),
], ids=["late_2018", "late_2019_jan", "late_2019_02_10", "year_2027", "year_2015", "offshore_fraud", "amended_reduction", "offshore_flag"])
def test_blocked_inputs_are_not_computed(monkeypatch, call, status):
    monkeypatch.setattr(K, "_call", _refuse_engine)
    result = call()
    assert result["status"] == status
    assert result["value"] is None and result["raw"] is None
    assert result["legal_correctness_verified"] is False and result["requires_review"] is True
    assert result["authorities"]


def test_known_error_cutoff_does_not_include_2019_02_11(monkeypatch):
    monkeypatch.setattr(K, "installed_version", lambda: K.PINNED_VERSION)
    monkeypatch.setattr(K, "_call", lambda *args, **kwargs: K._envelope("late_payment_penalty", K.COMPUTED, value=7, raw={}))
    result = K.late_payment_penalty(5_000_000, date(2019, 2, 11), date(2019, 2, 12))
    assert result["status"] == K.COMPUTED and result["value"] == 7


def test_wrong_version_blocks_before_engine(monkeypatch):
    monkeypatch.setattr(K, "_call", _refuse_engine)
    monkeypatch.setattr(K, "installed_version", lambda: "0.12.1")
    result = K.late_payment_penalty(5_000_000, date(2026, 1, 25), date(2026, 4, 25))
    assert result["status"] == K.BLOCKED_VERSION
    assert result["value"] is None and result["raw"] is None
    assert result["engine_version"] == "0.12.1"
    assert result["legal_correctness_verified"] is False and result["requires_review"] is True


def _common():
    return CommonInput(period_end=date(2026, 6, 30), common_input_tax=2_000_000, total_supply=100_000_000, exempt_supply=30_000_000)


@needs_engine
def test_common_input_matches_authoritative_vat_rules_kr():
    result = K.crosscheck_common_input(_common())
    assert result["decision"] == K.MATCH
    assert result["authoritative_rule"] == "price.vat_rules_kr.apportion_common_input"
    assert result["authoritative_value"] == result["external"]["value"] == 600_000
    assert result["legal_correctness_verified"] is False and result["requires_review"] is True
    assert result["engine_version"] == "0.12.2"
    assert result["authorities"]


def test_mismatch_goes_to_review(monkeypatch):
    monkeypatch.setattr(K, "vat_common_input_allocation", lambda *args, **kwargs: {
        "status": K.COMPUTED, "value": 1, "engine_version": K.PINNED_VERSION, "authorities": ["부가가치세법 시행령 제81조"],
        "legal_correctness_verified": False, "requires_review": True,
    })
    result = K.crosscheck_common_input(_common())
    assert result["decision"] == K.REVIEW
    assert result["authoritative_value"] == 600_000
    assert result["requires_review"] is True and result["legal_correctness_verified"] is False


def test_unpinned_engine_cannot_confirm_common_input(monkeypatch):
    monkeypatch.setattr(K, "_call", _refuse_engine)
    monkeypatch.setattr(K, "installed_version", lambda: None)
    result = K.crosscheck_common_input(_common())
    assert result["decision"] == K.REVIEW
    assert result["authoritative_value"] == 600_000
    assert result["external"]["status"] == K.BLOCKED_VERSION
    assert result["external"]["value"] is None
    assert result["legal_correctness_verified"] is False and result["requires_review"] is True


def test_runtime_entrypoints_do_not_load_the_crosscheck():
    root = Path(__file__).resolve().parents[1]
    for name in ("ai.py", "api.py", "worker.py", "cli.py"):
        text = (root / "price" / name).read_text(encoding="utf-8")
        assert "external_calc" not in text
        assert "korean_tax_calc" not in text
