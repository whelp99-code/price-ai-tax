from datetime import date
from pathlib import Path

import pytest

from price.db import DomainError
from price.external_calc import korean_tax_calc as K
from price.vat_rules_kr import CommonInput

needs_engine = pytest.mark.skipif(K.installed_version() != K.PINNED_VERSION, reason="optional korean-tax-calc-mcp==0.12.2 is not installed")


def _refuse_engine(*_args, **_kwargs):
    raise AssertionError("external engine must not be called")


def _common(period_end: date = date(2026, 6, 30)) -> CommonInput:
    return CommonInput(period_end=period_end, common_input_tax=2_000_000, total_supply=100_000_000, exempt_supply=30_000_000)


@needs_engine
def test_common_input_example_is_computed():
    result = K.vat_common_input_allocation(2_000_000, 100_000_000, 30_000_000, period_end=date(2026, 6, 30))
    assert result["status"] == K.COMPUTED, result
    assert result["value"] == 600_000 and type(result["value"]) is int
    assert result["engine_version"] == K.PINNED_VERSION == "0.12.2"
    assert result["legal_correctness_verified"] is False and result["requires_review"] is True
    assert result["authorities"]


@needs_engine
def test_common_input_matches_authoritative_vat_rules_kr():
    result = K.crosscheck_common_input(_common())
    assert result["decision"] == K.MATCH
    assert result["authoritative_rule"] == "price.vat_rules_kr.apportion_common_input"
    assert result["authoritative_value"] == result["external"]["value"] == 600_000
    assert result["legal_correctness_verified"] is False and result["requires_review"] is True
    assert result["engine_version"] == "0.12.2"


@pytest.mark.parametrize("args,reason", [
    ((2_000_000, 0, 0), "INVALID_INPUT_RELATION"),
    ((2_000_000, 100, 101), "INVALID_INPUT_RELATION"),
    ((-1, 100, 30), "INVALID_INPUT"),
    ((2_000_000, 100, True), "INVALID_INPUT"),
], ids=["zero_total", "exempt_over_total", "negative_tax", "bool_amount"])
def test_invalid_inputs_never_reach_the_engine(monkeypatch, args, reason):
    monkeypatch.setattr(K, "_call", _refuse_engine)
    result = K.vat_common_input_allocation(*args, period_end=date(2026, 6, 30))
    assert result["status"] == K.BLOCKED_UNSUPPORTED and result["reason"] == reason
    assert result["value"] is None and result["raw"] is None


@pytest.mark.parametrize("period_end", [date(2015, 12, 31), date(2027, 1, 1)], ids=["year_2015", "year_2027"])
def test_year_outside_window_is_blocked(monkeypatch, period_end):
    monkeypatch.setattr(K, "_call", _refuse_engine)
    result = K.vat_common_input_allocation(2_000_000, 100_000_000, 30_000_000, period_end=period_end)
    assert result["status"] == K.BLOCKED_UNSUPPORTED and result["reason"] == "YEAR_OUTSIDE_2016_2026"


def test_wrong_version_blocks_before_engine(monkeypatch):
    monkeypatch.setattr(K, "_call", _refuse_engine)
    monkeypatch.setattr(K, "installed_version", lambda: "0.12.1")
    result = K.vat_common_input_allocation(2_000_000, 100_000_000, 30_000_000, period_end=date(2026, 6, 30))
    assert result["status"] == K.BLOCKED_VERSION
    assert result["value"] is None and result["raw"] is None
    assert result["engine_version"] == "0.12.1"


def test_engine_exception_becomes_review_not_crash(monkeypatch):
    pytest.importorskip("korean_tax_calc_mcp")
    from korean_tax_calc_mcp import server

    def boom(*_args):
        raise ZeroDivisionError("engine failure")

    monkeypatch.setattr(K, "installed_version", lambda: K.PINNED_VERSION)
    monkeypatch.setattr(server, "vat_common_input_allocation", boom)
    result = K.crosscheck_common_input(_common())
    assert result["decision"] == K.REVIEW
    assert result["authoritative_value"] == 600_000
    assert result["external"]["status"] == K.BLOCKED_UNSUPPORTED
    assert result["external"]["reason"] == "ENGINE_EXCEPTION:ZeroDivisionError"


def test_mismatch_goes_to_review(monkeypatch):
    monkeypatch.setattr(K, "vat_common_input_allocation", lambda *args, **kwargs: {
        "status": K.COMPUTED, "value": 1, "engine_version": K.PINNED_VERSION,
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
    assert result["external"]["status"] == K.BLOCKED_VERSION and result["external"]["value"] is None


def test_internal_rule_error_is_not_replaced_by_engine(monkeypatch):
    monkeypatch.setattr(K, "vat_common_input_allocation", _refuse_engine)
    with pytest.raises(DomainError):
        K.crosscheck_common_input(_common(period_end=date(2025, 12, 31)))


def test_runtime_entrypoints_do_not_load_the_crosscheck():
    root = Path(__file__).resolve().parents[1]
    for name in ("ai.py", "api.py", "worker.py", "cli.py"):
        text = (root / "price" / name).read_text(encoding="utf-8")
        assert "external_calc" not in text
        assert "korean_tax_calc" not in text


def test_only_common_input_is_exposed():
    for name in ("invoice_penalty", "late_payment_penalty", "underreporting_penalty", "vat_bad_debt_credit"):
        assert not hasattr(K, name)
