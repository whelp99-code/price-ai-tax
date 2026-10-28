"""VAT cross-check against korean-tax-calc-mcp, pinned to 0.12.2.

The library is imported as plain functions. This module never calls the
package entrypoint, so no MCP stdio or HTTP server is started and the model
adapter is not given a calculation tool.

price.vat_rules_kr stays authoritative. The only comparison implemented here
is apportion_common_input versus vat_common_input_allocation. Invoice
penalties, late-payment penalty, under-reporting penalty, and bad-debt credit
have no calculator in vat_rules_kr (vat_returns only stores an already
reviewed adjustment amount), so those results are marked NOT_COMPARED.

Every result has legal_correctness_verified=False and requires_review=True.
A numeric MATCH means the integers agreed. It is not a legal sign-off.
"""
from __future__ import annotations

from datetime import date
from importlib import metadata

PINNED_VERSION = "0.12.2"
PACKAGE = "korean-tax-calc-mcp"
YEAR_MIN = 2016
YEAR_MAX = 2026
MONEY_MAX = 1_000_000_000_000
# 국세기본법 시행령 제27조의4 일 1만분의 3은 2019-02-11까지다.
# 0.12.2 server.late_payment_penalty는 calc_cit.late_payment_penalty로 연결되어
# 2022-02-15 이전 전 기간을 일 0.025%로만 계산한다 (예: 기한 2018-01-25,
# 5,000,000원, 90일 → 112,500. 일 0.03%이면 135,000).
# 법정납부기한이 2019-02-11보다 이르면 기산일(다음날)이 그 구간에 걸치므로
# 도구를 호출하지 않는다. calc_vat의 0.03% 구간으로 바꿔 계산하지 않는다.
LATE_PAYMENT_KNOWN_ERROR_BEFORE = date(2019, 2, 11)

BLOCKED_KNOWN_ERROR = "BLOCKED_KNOWN_ERROR"
BLOCKED_UNSUPPORTED = "BLOCKED_UNSUPPORTED"
BLOCKED_VERSION = "BLOCKED_VERSION_MISMATCH"
COMPUTED = "COMPUTED"
MATCH = "MATCH"
REVIEW = "REVIEW"
NOT_COMPARED = "NOT_COMPARED"

AUTHORITIES = {
    "invoice_penalty": ["부가가치세법 제60조②⑤⑥⑦"],
    "late_payment_penalty": ["국세기본법 제47조의4", "국세기본법 시행령 제27조의4"],
    "underreporting_penalty": ["국세기본법 제47조의3"],
    "vat_common_input_allocation": ["부가가치세법 시행령 제81조"],
    "vat_bad_debt_credit": ["부가가치세법 제45조①"],
}


def installed_version() -> str | None:
    try:
        return metadata.version(PACKAGE)
    except metadata.PackageNotFoundError:
        return None


def _authorities(tool: str, authorities: list[str] | None) -> list[str]:
    return list(AUTHORITIES[tool] if authorities is None else authorities)


def _envelope(tool: str, status: str, *, value: int | None = None, raw: dict | None = None, reason: str | None = None, authorities: list[str] | None = None) -> dict:
    return {
        "engine": PACKAGE,
        "engine_version": installed_version(),
        "pinned_version": PINNED_VERSION,
        "tool": tool,
        "status": status,
        "value": value,
        "raw": raw,
        "reason": reason,
        "authorities": _authorities(tool, authorities),
        "legal_correctness_verified": False,
        "requires_review": True,
    }


def _not_compared(result: dict) -> dict:
    return {**result, "internal_comparison": NOT_COMPARED, "internal_comparison_reason": "NO_VAT_RULES_KR_COUNTERPART"}


def _money(value: object) -> bool:
    return type(value) is int and 0 <= value <= MONEY_MAX


def _year_ok(*days: date) -> bool:
    return all(YEAR_MIN <= day.year <= YEAR_MAX for day in days)


def _version_block(tool: str, authorities: list[str] | None = None) -> dict | None:
    found = installed_version()
    if found != PINNED_VERSION:
        return _envelope(tool, BLOCKED_VERSION, reason=f"need {PACKAGE}=={PINNED_VERSION}, found {found}", authorities=authorities)
    return None


def _call(tool: str, key: str, *args: object, authorities: list[str] | None = None) -> dict:
    blocked = _version_block(tool, authorities)
    if blocked:
        return blocked
    import korean_tax_calc_mcp
    from korean_tax_calc_mcp import server

    module_version = getattr(korean_tax_calc_mcp, "__version__", None)
    if module_version != PINNED_VERSION:
        return _envelope(tool, BLOCKED_VERSION, reason=f"module version {module_version} != {PINNED_VERSION}", authorities=authorities)
    raw = getattr(server, tool)(*args)
    if not isinstance(raw, dict) or "error" in raw or not isinstance(raw.get("결과"), dict):
        return _envelope(tool, BLOCKED_UNSUPPORTED, raw=raw if isinstance(raw, dict) else None, reason="ENGINE_ERROR", authorities=authorities)
    value = raw["결과"].get(key)
    if type(value) is not int:
        return _envelope(tool, BLOCKED_UNSUPPORTED, raw=raw, reason="NON_INTEGER_RESULT", authorities=authorities)
    return _envelope(tool, COMPUTED, value=value, raw=raw, authorities=authorities)


def _invoice_authorities(kind: object) -> list[str]:
    if kind == "가공":
        return ["부가가치세법 제60조③"]
    return list(AUTHORITIES["invoice_penalty"])


def invoice_penalty(supply_amount: int, kind: str, supplied_on: date) -> dict:
    """세금계산서 가산세. vat_rules_kr에 대응 계산이 없어 값만 담고 비교하지 않는다."""
    authorities = _invoice_authorities(kind)
    if not _money(supply_amount) or type(kind) is not str or not kind or type(supplied_on) is not date:
        return _not_compared(_envelope("invoice_penalty", BLOCKED_UNSUPPORTED, reason="INVALID_INPUT", authorities=authorities))
    if not _year_ok(supplied_on):
        return _not_compared(_envelope("invoice_penalty", BLOCKED_UNSUPPORTED, reason="YEAR_OUTSIDE_2016_2026", authorities=authorities))
    blocked = _version_block("invoice_penalty", authorities)
    if blocked:
        return _not_compared(blocked)
    return _not_compared(_call("invoice_penalty", "가산세", supply_amount, kind, supplied_on.isoformat(), authorities=authorities))


def late_payment_penalty(unpaid_tax: int, due: date, until: date) -> dict:
    """납부지연가산세. 2019-02-11 이전 법정납부기한은 알려진 오류라 계산하지 않는다."""
    tool = "late_payment_penalty"
    if not _money(unpaid_tax) or type(due) is not date or type(until) is not date:
        return _not_compared(_envelope(tool, BLOCKED_UNSUPPORTED, reason="INVALID_INPUT"))
    if due < LATE_PAYMENT_KNOWN_ERROR_BEFORE:
        return _not_compared(_envelope(tool, BLOCKED_KNOWN_ERROR, reason="PERIOD_BEFORE_2019_02_11_ENGINE_USES_0.025_NOT_0.03"))
    if until < due:
        return _not_compared(_envelope(tool, BLOCKED_UNSUPPORTED, reason="UNTIL_BEFORE_DUE"))
    if not _year_ok(due, until):
        return _not_compared(_envelope(tool, BLOCKED_UNSUPPORTED, reason="YEAR_OUTSIDE_2016_2026"))
    blocked = _version_block(tool)
    if blocked:
        return _not_compared(blocked)
    return _not_compared(_call(tool, "가산세", unpaid_tax, due.isoformat(), until.isoformat()))


def underreporting_penalty(additional_tax: int, fraud: bool, *, tax_year: int, offshore: bool = False, amended_return_reduction: bool = False) -> dict:
    """과소신고가산세. 역외·수정신고 감면은 엔진 인자에 없어 일반/부정 세율로 떨어뜨리지 않는다."""
    tool = "underreporting_penalty"
    if (not _money(additional_tax) or type(fraud) is not bool or type(tax_year) is not int
            or type(offshore) is not bool or type(amended_return_reduction) is not bool):
        return _not_compared(_envelope(tool, BLOCKED_UNSUPPORTED, reason="INVALID_INPUT"))
    if not YEAR_MIN <= tax_year <= YEAR_MAX:
        return _not_compared(_envelope(tool, BLOCKED_UNSUPPORTED, reason="YEAR_OUTSIDE_2016_2026"))
    if offshore and fraud:
        return _not_compared(_envelope(tool, BLOCKED_UNSUPPORTED, reason="OFFSHORE_FRAUD_60_NOT_MODELLED"))
    if amended_return_reduction:
        return _not_compared(_envelope(tool, BLOCKED_UNSUPPORTED, reason="AMENDED_RETURN_REDUCTION_NOT_MODELLED"))
    if offshore:
        return _not_compared(_envelope(tool, BLOCKED_UNSUPPORTED, reason="OFFSHORE_FLAG_NOT_MODELLED"))
    blocked = _version_block(tool)
    if blocked:
        return _not_compared(blocked)
    return _not_compared(_call(tool, "가산세", additional_tax, fraud))


def vat_common_input_allocation(common_tax: int, total_supply: int, exempt_supply: int, *, period_end: date) -> dict:
    """공통매입세액 불공제분. 판정 비교는 crosscheck_common_input만 한다."""
    tool = "vat_common_input_allocation"
    if any(not _money(amount) for amount in (common_tax, total_supply, exempt_supply)) or type(period_end) is not date:
        return _envelope(tool, BLOCKED_UNSUPPORTED, reason="INVALID_INPUT")
    if not _year_ok(period_end):
        return _envelope(tool, BLOCKED_UNSUPPORTED, reason="YEAR_OUTSIDE_2016_2026")
    blocked = _version_block(tool)
    if blocked:
        return blocked
    return _call(tool, "불공제", common_tax, total_supply, exempt_supply)


def vat_bad_debt_credit(bad_debt_incl_vat: int, *, bad_debt_date: date, supply_receivable: bool = True) -> dict:
    """대손세액. vat_rules_kr에 대응 계산이 없어 비교하지 않는다."""
    tool = "vat_bad_debt_credit"
    if not _money(bad_debt_incl_vat) or type(bad_debt_date) is not date or type(supply_receivable) is not bool:
        return _not_compared(_envelope(tool, BLOCKED_UNSUPPORTED, reason="INVALID_INPUT"))
    if not _year_ok(bad_debt_date):
        return _not_compared(_envelope(tool, BLOCKED_UNSUPPORTED, reason="YEAR_OUTSIDE_2016_2026"))
    blocked = _version_block(tool)
    if blocked:
        return _not_compared(blocked)
    return _not_compared(_call(tool, "대손세액", bad_debt_incl_vat, supply_receivable))


def crosscheck_common_input(data: object) -> dict:
    """Compare price.vat_rules_kr.apportion_common_input with the external integer.

    The internal rule runs first. DomainError from that rule propagates: an
    out-of-window period is not replaced by the external number. Equal
    integers are MATCH. Any other external outcome is REVIEW. requires_review
    stays True either way.
    """
    from .vat_rules_kr import CommonInput, apportion_common_input

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
        "authorities": list(AUTHORITIES["vat_common_input_allocation"]),
        "legal_correctness_verified": False,
        "requires_review": True,
    }
