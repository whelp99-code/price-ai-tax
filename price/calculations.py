"""Deterministic mathematical helpers. NOT a complete Korean tax rule pack."""
from __future__ import annotations
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from .db import DomainError
from .accounts import ACCOUNT_INFO


def strict_money(x, *, positive=False) -> int:
    if type(x) is not int or x < (1 if positive else 0) or x > 1_000_000_000_000:
        raise DomainError("INVALID_MONEY", "금액은 허용 범위의 정수여야 합니다.", 422)
    return x


def rate_tax(base: int, rate: str, rounding: str = "down") -> dict:
    strict_money(base)
    try:
        r = Decimal(rate)
    except Exception:
        raise DomainError("INVALID_RATE", "유효하지 않은 세율입니다.", 422)
    if not r.is_finite() or r < 0 or r > 1 or rounding not in ("down", "half_up"):
        raise DomainError("INVALID_RATE", "유효하지 않은 세율/반올림입니다.", 422)
    exact = Decimal(base) * r
    value = int(exact.quantize(Decimal("1"), rounding=ROUND_DOWN if rounding == "down" else ROUND_HALF_UP))
    return {"base": base, "rate": str(r), "unrounded": str(exact), "rounding": rounding, "tax": value, "scope": "mathematical_simulation"}


def progressive_tax(base: int, brackets: list[dict], rounding: str = "down") -> dict:
    strict_money(base)
    if not brackets or len(brackets) > 20 or brackets[-1].get("upper") is not None:
        raise DomainError("INVALID_BRACKETS", "마지막 구간은 상한 없이 지정해야 합니다.", 422)
    lower, exact, trace = 0, Decimal(0), []
    for i, b in enumerate(brackets):
        upper = b.get("upper")
        if upper is not None and (type(upper) is not int or upper <= lower or upper > 1_000_000_000_000):
            raise DomainError("INVALID_BRACKETS", "누진구간은 증가하는 정수 상한이어야 합니다.", 422)
        if upper is None and i != len(brackets)-1:
            raise DomainError("INVALID_BRACKETS", "무제한 구간은 마지막에만 가능합니다.", 422)
        rate_tax(0, b.get("rate", ""), rounding)
        taxable = max(0, min(base, upper if upper is not None else base) - lower)
        amount = Decimal(taxable) * Decimal(b["rate"])
        exact += amount
        trace.append({"lower": lower, "upper": upper, "rate": b["rate"], "taxable": taxable, "unrounded": str(amount)})
        if upper is not None:
            lower = upper
    total = int(exact.quantize(Decimal("1"), rounding=ROUND_DOWN if rounding == "down" else ROUND_HALF_UP))
    return {"base": base, "brackets": trace, "tax": total, "rounding": rounding, "scope": "mathematical_simulation_not_korean_corporate_return"}


def straight_line_schedule(cost: int, salvage: int, months: int) -> list[int]:
    strict_money(cost); strict_money(salvage)
    if salvage > cost or type(months) is not int or months < 1 or months > 1200:
        raise DomainError("INVALID_ASSET", "잔존가치와 기간을 확인하세요.", 422)
    base = cost - salvage
    monthly = base // months
    return [monthly] * (months - 1) + [base - monthly * (months - 1)]


def validate_lines(lines: list[dict]) -> None:
    if len(lines) < 2:
        raise DomainError("EMPTY_JOURNAL", "차변과 대변이 필요합니다.", 422)
    debit = credit = 0
    for line in lines:
        if line.get('account') not in ACCOUNT_INFO:
            raise DomainError('UNKNOWN_ACCOUNT','검증된 장부 계정이 아닙니다.',422)
        strict_money(line["amount"], positive=True)
        if line["side"] == "debit":
            debit += line["amount"]
        elif line["side"] == "credit":
            credit += line["amount"]
        else:
            raise DomainError("INVALID_SIDE", "차변/대변을 확인하세요.", 422)
    if debit != credit:
        raise DomainError("UNBALANCED_JOURNAL", "차변과 대변 합계가 일치하지 않습니다.", 422, {"debit": debit, "credit": credit})
