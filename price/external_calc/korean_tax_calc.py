"""Common input VAT cross-check against korean-tax-calc-mcp, pinned to 0.12.2.

The library is imported as plain functions. This module never calls the
package entrypoint, so no MCP stdio or HTTP server is started and the model
adapter is not given a calculation tool.

price.vat_rules_kr stays authoritative. The only comparison implemented here
is apportion_common_input versus vat_common_input_allocation. Other engine
tools from the original experiment are not exposed.

Every result has legal_correctness_verified=False and requires_review=True.
A numeric MATCH means the integers agreed. It is not a legal sign-off.
"""
from __future__ import annotations

from datetime import date
from importlib import metadata

PINNED_VERSION = "0.12.2"
PACKAGE = "korean-tax-calc-mcp"
TOOL = "vat_common_input_allocation"
YEAR_MIN = 2016
YEAR_MAX = 2026
MONEY_MAX = 1_000_000_000_000

BLOCKED_UNSUPPORTED = "BLOCKED_UNSUPPORTED"
BLOCKED_VERSION = "BLOCKED_VERSION_MISMATCH"
COMPUTED = "COMPUTED"
MATCH = "MATCH"
REVIEW = "REVIEW"

AUTHORITIES = ["부가가치세법 시행령 제81조"]


def installed_version() -> str | None:
    try:
        return metadata.version(PACKAGE)
    except metadata.PackageNotFoundError:
        return None


def _envelope(status: str, *, value: int | None = None, raw: dict | None = None, reason: str | None = None) -> dict:
    return {
        "engine": PACKAGE,
        "engine_version": installed_version(),
        "pinned_version": PINNED_VERSION,
        "tool": TOOL,
        "status": status,
        "value": value,
        "raw": raw,
        "reason": reason,
        "authorities": list(AUTHORITIES),
        "legal_correctness_verified": False,
        "requires_review": True,
    }


def _money(value: object) -> bool:
    return type(value) is int and 0 <= value <= MONEY_MAX


def _version_block() -> dict | None:
    found = installed_version()
    if found != PINNED_VERSION:
        return _envelope(BLOCKED_VERSION, reason=f"need {PACKAGE}=={PINNED_VERSION}, found {found}")
    return None


def _call(*args: object) -> dict:
    blocked = _version_block()
    if blocked:
        return blocked
    try:
        import korean_tax_calc_mcp
        from korean_tax_calc_mcp import server

        module_version = getattr(korean_tax_calc_mcp, "__version__", None)
        if module_version != PINNED_VERSION:
            return _envelope(BLOCKED_VERSION, reason=f"module version {module_version} != {PINNED_VERSION}")
        raw = server.vat_common_input_allocation(*args)
    except Exception as exc:  # the engine is advisory; its failure must not stop the authoritative rule
        return _envelope(BLOCKED_UNSUPPORTED, reason=f"ENGINE_EXCEPTION:{type(exc).__name__}")
    if not isinstance(raw, dict) or "error" in raw or not isinstance(raw.get("결과"), dict):
        return _envelope(BLOCKED_UNSUPPORTED, raw=raw if isinstance(raw, dict) else None, reason="ENGINE_ERROR")
    value = raw["결과"].get("불공제")
    if type(value) is not int:
        return _envelope(BLOCKED_UNSUPPORTED, raw=raw, reason="NON_INTEGER_RESULT")
    return _envelope(COMPUTED, value=value, raw=raw)


def vat_common_input_allocation(common_tax: int, total_supply: int, exempt_supply: int, *, period_end: date) -> dict:
    """공통매입세액 불공제분. 판정 비교는 crosscheck_common_input만 한다."""
    if any(not _money(amount) for amount in (common_tax, total_supply, exempt_supply)) or type(period_end) is not date:
        return _envelope(BLOCKED_UNSUPPORTED, reason="INVALID_INPUT")
    if total_supply == 0 or exempt_supply > total_supply:
        return _envelope(BLOCKED_UNSUPPORTED, reason="INVALID_INPUT_RELATION")
    if not YEAR_MIN <= period_end.year <= YEAR_MAX:
        return _envelope(BLOCKED_UNSUPPORTED, reason="YEAR_OUTSIDE_2016_2026")
    blocked = _version_block()
    if blocked:
        return blocked
    return _call(common_tax, total_supply, exempt_supply)


def crosscheck_common_input(data: object) -> dict:
    """Compare price.vat_rules_kr.apportion_common_input with the external integer.

    The internal rule runs first. DomainError from that rule propagates: an
    out-of-window period is not replaced by the external number. Equal
    integers are MATCH. Any other external outcome is REVIEW. requires_review
    stays True either way.
    """
    from ..vat_rules_kr import CommonInput, apportion_common_input

    parsed = CommonInput.model_validate(data)
    authoritative = apportion_common_input(parsed)
    external = vat_common_input_allocation(
        parsed.common_input_tax, parsed.total_supply, parsed.exempt_supply, period_end=parsed.period_end)
    agreed = external["status"] == COMPUTED and external["value"] == authoritative["nondeductible"]
    return {
        "decision": MATCH if agreed else REVIEW,
        "authoritative_rule": "price.vat_rules_kr.apportion_common_input",
        "authoritative": authoritative,
        "authoritative_value": authoritative["nondeductible"],
        "external": external,
        "engine": PACKAGE,
        "engine_version": external["engine_version"],
        "pinned_version": PINNED_VERSION,
        "authorities": list(AUTHORITIES),
        "legal_correctness_verified": False,
        "requires_review": True,
    }
