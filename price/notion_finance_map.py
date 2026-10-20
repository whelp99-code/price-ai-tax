"""Map a Notion finance snapshot row to a price transaction draft.

Amounts are never rewritten. A row whose own figures disagree, or whose
figures disagree with an external approval, is HOLD.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from pydantic import JsonValue

from .models import TransactionInput

CLASSIFICATION: dict[
    str,
    tuple[Literal["invoice", "bank"], Literal["sale", "purchase", "receipt", "payment"]],
] = {
    "매출": ("invoice", "sale"),
    "매입": ("invoice", "purchase"),
    "기타입금": ("bank", "receipt"),
    "기타출금": ("bank", "payment"),
}

_DATE = re.compile(r"(20\d{2}-\d{2}-\d{2})")
_APPROVAL_EXACT = re.compile(r"^[0-9]{24}$")
_INVOICE_KEY = re.compile(r"^invoice\|([0-9]{24})$")

SOURCE = "notion"


@dataclass(frozen=True)
class Hold:
    page_id: str
    classification: str
    reason: str
    net: int | None = None
    tax: int | None = None
    gross: int | None = None
    approval_tail: str = ""
    counterparty: str = ""
    external_gross: int | None = None

    @property
    def held(self) -> bool:
        return True


@dataclass(frozen=True)
class Mapped:
    page_id: str
    classification: str
    kind: str
    direction: str
    net: int
    tax: int
    gross: int
    approval: str | None
    keys: dict[str, str]
    draft: TransactionInput

    @property
    def held(self) -> bool:
        return False


@dataclass
class MapReport:
    rows: int = 0
    by_class: dict[str, int] = field(default_factory=dict)
    loaded: list[Mapped] = field(default_factory=list)
    holds: list[Hold] = field(default_factory=list)
    hometax_separate: list[dict[str, str]] = field(default_factory=list)

    def purchase_notion_gross(self) -> int:
        total = 0
        for item in (*self.loaded, *self.holds):
            if item.classification == "매입" and item.gross is not None:
                total += item.gross
        return total

    def purchase_loaded_gross(self) -> int:
        return sum(item.gross for item in self.loaded if item.direction == "purchase")


def _money(value: JsonValue, label: str) -> int | Hold:
    if isinstance(value, bool) or not isinstance(value, int):
        return Hold("", "", f"{label} is not an integer; not fixed")
    if value < 0:
        return Hold("", "", f"{label} is negative; not fixed")
    return value


def _date_from(*texts: str | None) -> date | None:
    for text in texts:
        if not text:
            continue
        match = _DATE.search(str(text))
        if match:
            return date.fromisoformat(match.group(1))
    return None


def _labeled_approval_token(memo: str) -> tuple[str | None, bool]:
    """Return the token immediately after an explicit Hometax approval label.

    A later bare 24-digit run is never a substitute for a bad labeled token.
    """
    labeled = re.search(r"홈택스\s*승인번호\s*[:：]", memo or "")
    if not labeled:
        return None, False
    rest = (memo or "")[labeled.end():]
    token_match = re.match(r"\s*([0-9A-Za-z-]+)", rest)
    if not token_match:
        return None, True
    token = token_match.group(1)
    if _APPROVAL_EXACT.fullmatch(token):
        return token, True
    return None, True


def _approval(natural_key: str, memo: str) -> tuple[str | None, str | None]:
    """Return (approval, conflict_reason). Never picks a winner."""
    from_key = None
    match = _INVOICE_KEY.match(natural_key.strip())
    if match:
        from_key = match.group(1)
    elif natural_key.strip().lower().startswith("invoice|"):
        return None, "approval is not exactly 24 digits; not loaded"
    from_memo, labeled = _labeled_approval_token(memo or "")
    if labeled and from_memo is None:
        return None, "approval is not a complete 24-digit number; not loaded"
    if from_key and from_memo and from_key != from_memo:
        return None, "approval in natural key and memo disagree; not fixed"
    return from_key or from_memo, None


def _counterparty(props: Mapping[str, JsonValue]) -> str:
    named = props.get("거래처")
    if isinstance(named, str) and named.strip():
        return named.strip()[:160]
    title = str(props.get("거래명") or "").strip()
    head = title.split("|", 1)[0].strip()
    return (head or title or "unknown")[:160]


def map_row(row: Mapping[str, JsonValue]) -> Mapped | Hold:
    """Map one snapshot finance object. Does not read a database."""
    page_id = str(row.get("sourceId") or "").strip()
    properties = row.get("properties")
    props = properties if isinstance(properties, Mapping) else row
    classification = str(props.get("구분") or "").strip()
    if not page_id:
        return Hold("", classification, "missing notion page id; not fixed")
    pair = CLASSIFICATION.get(classification)
    if pair is None:
        return Hold(page_id, classification, f"unknown classification {classification}; not fixed")
    kind, direction = pair
    raw_tax = props.get("부가세")
    if kind == "bank" and raw_tax in (None, ""):
        raw_tax = 0
    amounts: list[int] = []
    for label, value in (("net", props.get("금액")), ("tax", raw_tax), ("gross", props.get("합계"))):
        parsed = _money(value, label)
        if isinstance(parsed, Hold):
            return Hold(page_id, classification, parsed.reason)
        amounts.append(parsed)
    net, tax, gross = amounts
    if net + tax != gross:
        return Hold(
            page_id,
            classification,
            "net+tax!=gross; conflicting values not fixed",
            net,
            tax,
            gross,
        )
    if kind == "bank" and tax:
        return Hold(page_id, classification, "tax on bank-only row; not fixed", net, tax, gross)
    approval, approval_conflict = _approval(str(props.get("자연키") or ""), str(props.get("메모") or ""))
    if approval_conflict:
        return Hold(page_id, classification, approval_conflict, net, tax, gross)
    if kind == "invoice" and tax == 0:
        return Hold(
            page_id,
            classification,
            "invoice tax is 0; exempt/zero not distinguishable; not fixed",
            net,
            tax,
            gross,
        )
    if kind == "invoice" and tax * 10 != net:
        return Hold(
            page_id,
            classification,
            "invoice tax is not 10 percent of net; category not inferred; not fixed",
            net,
            tax,
            gross,
        )
    processed_date = props.get("실제 처리일")
    event_date = _date_from(
        processed_date if isinstance(processed_date, str) else None,
        str(props.get("거래명") or ""),
        str(props.get("자연키") or ""),
        str(props.get("메모") or ""),
    )
    if event_date is None:
        return Hold(page_id, classification, "no event date; not fixed", net, tax, gross)
    tax_category = "standard" if kind == "invoice" else "unknown"
    keys = {"notion-page": page_id}
    if approval:
        keys["hometax-approval"] = approval
    draft = TransactionInput(
        external_id=f"notion-page:{page_id}",
        source=SOURCE,
        kind=kind,
        direction=direction,
        event_date=event_date,
        counterparty=_counterparty(props),
        net=net,
        tax=tax,
        gross=gross,
        tax_category=tax_category,
        deductible=None,
    )
    return Mapped(page_id, classification, kind, direction, net, tax, gross, approval, keys, draft)


def apply_external_amount_conflict(
    item: Mapped | Hold,
    external: Mapping[str, tuple[int, int, int]],
) -> Mapped | Hold:
    """Hold a purchase whose approval exists externally with different amounts.

    Does not replace Notion figures with the external figures.
    """
    if isinstance(item, Hold) or item.direction != "purchase" or not item.approval:
        return item
    if item.approval not in external:
        return item
    observed = external[item.approval]
    if observed == (item.net, item.tax, item.gross):
        return item
    return Hold(
        item.page_id,
        item.classification,
        "amounts conflict with external approval; notion 금액 treated as given, not fixed",
        item.net,
        item.tax,
        item.gross,
        approval_tail=item.approval[-4:],
        counterparty=item.draft.counterparty,
        external_gross=observed[2],
    )


def purchase_match_verdict(loaded_gross: int, hometax_gross: int, holds: list[Hold]) -> dict[str, str | int]:
    """PASS only when loaded purchase gross equals the Hometax total.

    HOLD_EXPLAINS is a separate field. It is never a pass.
    """
    gap = hometax_gross - loaded_gross
    status = "PASS" if gap == 0 else "FAIL"
    parts: list[str] = []
    explained = 0
    for hold in holds:
        if hold.classification != "매입" or hold.external_gross is None or hold.gross is None:
            continue
        explained += hold.external_gross
        label = hold.counterparty or "held"
        parts.append(
            f"HOLD_EXPLAINS: {label} …{hold.approval_tail} held {hold.gross:,} vs Hometax {hold.external_gross:,}"
        )
    explains = ""
    if status == "FAIL" and parts:
        closes = explained == gap
        tail = "gap closes if resolved" if closes else f"explained {explained:,} does not equal gap {gap:,}"
        explains = "; ".join(parts) + f" → {tail}"
    return {"status": status, "gap": gap, "hold_explains": explains}


def map_snapshot(
    rows: list[Mapping[str, JsonValue]],
    external_purchases: Mapping[str, tuple[int, int, int]] | None = None,
) -> MapReport:
    report = MapReport(rows=len(rows))
    external_purchases = external_purchases or {}
    for row in rows:
        item = apply_external_amount_conflict(map_row(row), external_purchases)
        report.by_class[item.classification] = report.by_class.get(item.classification, 0) + 1
        if isinstance(item, Hold):
            report.holds.append(item)
        else:
            report.loaded.append(item)
    return report
