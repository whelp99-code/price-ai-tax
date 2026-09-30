"""Map a Notion finance snapshot row to a price transaction draft.

Amounts are never rewritten. A row whose own figures disagree, or whose
figures disagree with an external approval, is HOLD.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping

from .models import TransactionInput

CLASSIFICATION = {
    "매출": ("invoice", "sale"),
    "매입": ("invoice", "purchase"),
    "기타입금": ("bank", "receipt"),
    "기타출금": ("bank", "payment"),
}

_DATE = re.compile(r"(20\d{2}-\d{2}-\d{2})")
_APPROVAL_MEMO = re.compile(r"홈택스 승인번호:\s*([0-9]{8,24})")
_INVOICE_KEY = re.compile(r"^invoice\|([0-9]{8,24})$")

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

    def purchase_notion_gross(self) -> int:
        total = 0
        for item in (*self.loaded, *self.holds):
            if item.classification == "매입" and item.gross is not None:
                total += item.gross
        return total

    def purchase_loaded_gross(self) -> int:
        return sum(item.gross for item in self.loaded if item.direction == "purchase")


def _money(value: Any, label: str) -> int | Hold:
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


def _approval(natural_key: str, memo: str) -> tuple[str | None, str | None]:
    """Return (approval, conflict_reason). Never picks a winner."""
    from_key = None
    match = _INVOICE_KEY.match(natural_key.strip())
    if match:
        from_key = match.group(1)
    from_memo = None
    memo_match = _APPROVAL_MEMO.search(memo or "")
    if memo_match:
        from_memo = memo_match.group(1)
    if from_key and from_memo and from_key != from_memo:
        return None, "approval in natural key and memo disagree; not fixed"
    return from_key or from_memo, None


def _counterparty(props: Mapping[str, Any]) -> str:
    named = props.get("거래처")
    if isinstance(named, str) and named.strip():
        return named.strip()[:160]
    title = str(props.get("거래명") or "").strip()
    head = title.split("|", 1)[0].strip()
    return (head or title or "unknown")[:160]


def map_row(row: Mapping[str, Any]) -> Mapped | Hold:
    """Map one snapshot finance object. Does not read a database."""
    page_id = str(row.get("sourceId") or "").strip()
    props = row.get("properties") if isinstance(row.get("properties"), Mapping) else row
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
    amounts = []
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
    event_date = _date_from(
        props.get("실제 처리일") if isinstance(props.get("실제 처리일"), str) else None,
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
    )


def map_snapshot(
    rows: list[Mapping[str, Any]],
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
