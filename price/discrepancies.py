"""Read-only discrepancy source-revision freshness.

Compares revision markers already stored on a quarantine or expert review
with the current saved object in the same company. A missing marker stays
unknown. This does not read a live provider, post a journal, or bind a
counterpart by an approval tail.
"""
from __future__ import annotations

from typing import Final, TypedDict

from .db import DomainError
from .review_cases import ReviewCases
from .service import WRITERS

PAGE_NAMESPACE: Final = "transaction:notion"
QUARANTINE_KIND: Final = "notion_quarantine"
REVIEW_KIND: Final = "expert_review"
_SOURCE_STALE: Final = frozenset({
    "SOURCE_CHANGED",
    "REVIEW_SOURCE_CHANGED",
    "REVIEW_FACT_CHANGED",
    "NOT_FOUND",
})
_SOURCE_INVALIDATION: Final = frozenset({"SOURCE_CHANGED", "REVIEW_SOURCE_CHANGED", "NOT_FOUND"})
INVALIDATED_STATE: Final = "SOURCE_INVALIDATED"


class MoneyRow(TypedDict):
    net: str | None
    tax: str | None
    gross: str | None


class AmountBlock(TypedDict):
    incoming: MoneyRow
    saved: MoneyRow | None


class InboxItem(TypedDict):
    id: str
    kind: str
    revision: int
    reason: str | None
    state: str | None
    freshness: str
    freshness_reason: str | None
    resolved: bool
    observed_at: str | None
    source_revision: str | None
    saved_source_revision: str | None
    page_id: str | None
    saved_id: str | None
    differences: MoneyRow | None
    amounts: AmountBlock | None
    professional_credential_verified: bool
    limitations: list[str]


class InboxReport(TypedDict):
    items: list[InboxItem]
    read_only: bool
    live_source_checked: bool


class InvalidationResult(TypedDict):
    id: str
    invalidated: bool
    state: str
    revision: int


def _required_text(value: object) -> str:
    if isinstance(value, str) and value:
        return value
    raise DomainError("SOURCE_RECORD_INVALID", "저장된 기록 식별자가 없습니다.")


def _required_revision(value: object) -> int:
    if type(value) is int:
        return value
    raise DomainError("SOURCE_RECORD_INVALID", "저장된 개정 번호가 없습니다.")


def _optional_text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None


def krw(value: object) -> int | None:
    if type(value) is not int:
        return None
    return value


def revision_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def freshness_of(observed: str | None, saved: str | None) -> str:
    if observed is None or saved is None:
        return "UNKNOWN"
    if observed != saved:
        return "STALE"
    return "STORED_UNCHANGED"


def money_text(value: object) -> str | None:
    amount = krw(value)
    if amount is None:
        return None
    return str(amount)


def _money_pair(incoming: object, saved: object) -> str | None:
    left, right = krw(incoming), krw(saved)
    if left is None or right is None:
        return None
    return str(left - right)


class Discrepancies:
    def __init__(self, service):
        self.service = service
        self.db = service.db
        self.reviews = ReviewCases(service)

    def list(self, actor, company) -> InboxReport:
        with self.db.connect() as c:
            c.execute("BEGIN")
            try:
                self.service.auth.guard(c, actor, company)
                items = [
                    self._quarantine_item(c, company, row)
                    for row in self.db.list(c, company, QUARANTINE_KIND)
                ]
                reviews = self.db.list(c, company, REVIEW_KIND)
                source_context = (
                    self.reviews.work._source_context(c, company)
                    if any(row.get("state") != INVALIDATED_STATE for row in reviews)
                    else None
                )
                items.extend(
                    self._review_item(c, company, row, source_context)
                    for row in reviews
                )
                report = InboxReport(items=items, read_only=True, live_source_checked=False)
            finally:
                c.rollback()
        return report

    def _saved_page(self, c, company: str, page_id: str) -> dict[str, object] | None:
        row = c.execute(
            "SELECT o.* FROM external_ids x "
            "JOIN objects o ON o.company_id=x.company_id AND o.id=x.object_id AND o.kind='transaction' "
            "WHERE x.company_id=? AND x.namespace=? AND x.external_id=?",
            (company, PAGE_NAMESPACE, "notion-page:" + page_id),
        ).fetchone()
        if row is None:
            return None
        return self.db.unpack(row)

    def _quarantine_item(self, c, company: str, row: dict[str, object]) -> InboxItem:
        page_id = revision_text(row.get("page_id"))
        saved = self._saved_page(c, company, page_id) if page_id else None
        observed = revision_text(row.get("last_edited_time"))
        raw_provenance = saved.get("notion_provenance") if saved else None
        provenance = raw_provenance if isinstance(raw_provenance, dict) else {}
        saved_revision = revision_text(provenance.get("last_edited_time")) if saved else None
        freshness = freshness_of(observed, saved_revision) if saved else "UNKNOWN"
        limitations: list[str] = []
        differences: MoneyRow | None = None
        if saved is None:
            limitations.append("saved_source_missing")
        else:
            limitations.append("same_page_saved_record_not_conflict_pair")
            differences = MoneyRow(
                net=_money_pair(row.get("net"), saved.get("net")),
                tax=_money_pair(row.get("tax"), saved.get("tax")),
                gross=_money_pair(row.get("gross"), saved.get("gross")),
            )
            if differences["net"] == "0" and differences["tax"] == "0" and differences["gross"] == "0":
                limitations.append("zero_delta_not_resolved")
        incoming_net, incoming_tax, incoming_gross = krw(row.get("net")), krw(row.get("tax")), krw(row.get("gross"))
        saved_amounts = None if saved is None else MoneyRow(
            net=money_text(saved.get("net")),
            tax=money_text(saved.get("tax")),
            gross=money_text(saved.get("gross")),
        )
        return InboxItem(
            id=_required_text(row.get("id")),
            kind=QUARANTINE_KIND,
            revision=_required_revision(row.get("revision")),
            reason=_optional_text(row.get("reason")),
            state="QUARANTINED",
            freshness=freshness,
            freshness_reason=None,
            resolved=False,
            observed_at=_optional_text(row.get("updated_at")),
            source_revision=observed,
            saved_source_revision=saved_revision,
            page_id=page_id,
            saved_id=_required_text(saved.get("id")) if saved else None,
            differences=differences,
            amounts=AmountBlock(
                incoming=MoneyRow(
                    net=None if incoming_net is None else str(incoming_net),
                    tax=None if incoming_tax is None else str(incoming_tax),
                    gross=None if incoming_gross is None else str(incoming_gross),
                ),
                saved=saved_amounts,
            ),
            professional_credential_verified=False,
            limitations=limitations,
        )

    def _review_item(self, c, company: str, row: dict[str, object], source_context) -> InboxItem:
        ident = _required_text(row.get("id"))
        revision = _required_revision(row.get("revision"))
        freshness_reason = "SOURCE_INVALIDATED" if row.get("state") == INVALIDATED_STATE else None
        if freshness_reason is None:
            try:
                self.reviews._fresh(c, company, ident, revision, source_context=source_context)
            except DomainError as error:
                if error.code == "REVIEW_FACT_CHANGED":
                    freshness_reason = "FACT_CHANGED"
                elif error.code in _SOURCE_INVALIDATION:
                    freshness_reason = "SOURCE_CHANGED"
                else:
                    raise
        freshness = "STALE" if freshness_reason is not None else "CURRENT"
        verified = row.get("professional_credential_verified") is True
        return InboxItem(
            id=ident,
            kind=REVIEW_KIND,
            revision=revision,
            reason=_optional_text(row.get("reason")),
            state=_optional_text(row.get("state")),
            freshness=freshness,
            freshness_reason=freshness_reason,
            resolved=False,
            observed_at=_optional_text(row.get("updated_at")),
            source_revision=revision_text(row.get("source_context_hash")),
            saved_source_revision=None,
            page_id=None,
            saved_id=_optional_text(row.get("event_id")),
            differences=None,
            amounts=None,
            professional_credential_verified=verified,
            limitations=["live_source_not_read"],
        )

    def invalidate_changed_source(self, actor, company: str, ident: str) -> InvalidationResult:
        with self.db.transaction() as c:
            self.service.auth.guard(c, actor, company, WRITERS)
            review = self.db.get(c, company, REVIEW_KIND, ident)
            current_state = _optional_text(review.get("state")) or ""
            revision = _required_revision(review.get("revision"))
            if current_state == INVALIDATED_STATE:
                return InvalidationResult(id=ident, invalidated=False, state=current_state, revision=revision)
            try:
                self.reviews._fresh(c, company, ident, revision)
            except DomainError as error:
                if error.code == "REVIEW_FACT_CHANGED":
                    return InvalidationResult(id=ident, invalidated=False, state=current_state, revision=revision)
                if error.code not in _SOURCE_INVALIDATION:
                    raise
                row = self.db.replace(
                    c, company, REVIEW_KIND, ident,
                    {**review, "state": INVALIDATED_STATE, "source_invalidated": True,
                     "invalidated_from_state": current_state, "professional_credential_verified": False,
                     "accounting_posted": False},
                    revision,
                )
                self.db.audit(c, company, actor.id, "review.source_invalidated", ident, {"from_state": current_state})
                return InvalidationResult(
                    id=ident, invalidated=True, state=INVALIDATED_STATE, revision=_required_revision(row.get("revision")),
                )
            return InvalidationResult(id=ident, invalidated=False, state=current_state, revision=revision)
