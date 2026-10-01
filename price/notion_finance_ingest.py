"""Apply a Notion finance snapshot as transaction drafts only.

Does not create journals, file returns, or call the network.
Amount mismatches against an existing hometax-approval key are quarantined.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from .auth import Actor
from .db import Database, digest
from .models import TransactionInput
from .notion_finance_map import Hold, Mapped, MapReport
from .service import Service

NS_PAGE = "transaction:notion"
NS_APPROVAL = "hometax-approval"
NS_QUARANTINE = "notion-quarantine"
KIND_QUARANTINE = "notion_quarantine"


def last_edited_time(row: Mapping[str, Any]) -> str:
    direct = row.get("lastEditedTime") or row.get("last_edited_time")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    props = row.get("properties") if isinstance(row.get("properties"), Mapping) else {}
    for key in ("last_edited_time", "최종 편집 일시"):
        value = props.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def edited_index(rows: list[Mapping[str, Any]]) -> dict[str, str]:
    found: dict[str, str] = {}
    for row in rows:
        page_id = str(row.get("sourceId") or "").strip()
        if page_id:
            found[page_id] = last_edited_time(row)
    return found


def _tx_fields(body: Mapping[str, Any]) -> dict[str, Any]:
    return {key: body[key] for key in TransactionInput.model_fields if key in body}


def _same_business(body: Mapping[str, Any], draft: TransactionInput) -> bool:
    current = TransactionInput.model_validate(_tx_fields(body)).model_dump(mode="json")
    return current == draft.model_dump(mode="json")


def _journal_blocks(c, company: str, object_id: str) -> bool:
    row = c.execute(
        """SELECT 1 FROM objects
           WHERE company_id=? AND kind='journal'
             AND json_extract(body,'$.transaction_id')=?
             AND json_extract(body,'$.state')!='SUPERSEDED'""",
        (company, object_id),
    ).fetchone()
    return row is not None


def _approval_owner(c, company: str, approval: str) -> str | None:
    row = c.execute(
        "SELECT object_id FROM external_ids WHERE company_id=? AND namespace=? AND external_id=?",
        (company, NS_APPROVAL, approval),
    ).fetchone()
    return None if row is None else row["object_id"]


def _stored_amounts(c, company: str, object_id: str) -> tuple[int, int, int] | None:
    row = c.execute(
        "SELECT body FROM objects WHERE company_id=? AND kind='transaction' AND id=?",
        (company, object_id),
    ).fetchone()
    if row is None:
        return None
    body = json.loads(row["body"])
    return int(body["net"]), int(body["tax"]), int(body["gross"])


def _page_object(c, company: str, external_id: str) -> str | None:
    row = c.execute(
        "SELECT object_id FROM external_ids WHERE company_id=? AND namespace=? AND external_id=?",
        (company, NS_PAGE, external_id),
    ).fetchone()
    return None if row is None else row["object_id"]


def _upsert_quarantine(db: Database, actor: Actor, company: str, page_id: str, body: dict) -> None:
    with db.transaction() as c:
        old = c.execute(
            "SELECT object_id FROM external_ids WHERE company_id=? AND namespace=? AND external_id=?",
            (company, NS_QUARANTINE, page_id),
        ).fetchone()
        stored = {**body, "author": actor.id}
        if old:
            current = db.get(c, company, KIND_QUARANTINE, old["object_id"])
            if all(current.get(key) == body[key] for key in body):
                return
            db.replace(c, company, KIND_QUARANTINE, current["id"], stored, current["revision"])
            return
        rec = db.create(c, company, KIND_QUARANTINE, stored)
        c.execute(
            "INSERT INTO external_ids VALUES(?,?,?,?,?)",
            (company, NS_QUARANTINE, page_id, digest(body), rec["id"]),
        )


def _stamp_provenance(db: Database, company: str, object_id: str, provenance: dict[str, str]) -> bool:
    with db.transaction() as c:
        current = db.get(c, company, "transaction", object_id)
        if current.get("notion_provenance") == provenance:
            return False
        db.replace(
            c,
            company,
            "transaction",
            object_id,
            {
                **_tx_fields(current),
                "source_hash": current.get("source_hash"),
                "author": current.get("author"),
                "notion_provenance": provenance,
            },
            current["revision"],
        )
    return True


def _link_approval(db: Database, company: str, approval: str, object_id: str, body_hash: str) -> None:
    with db.transaction() as c:
        owner = _approval_owner(c, company, approval)
        if owner is None:
            c.execute(
                "INSERT INTO external_ids VALUES(?,?,?,?,?)",
                (company, NS_APPROVAL, approval, body_hash, object_id),
            )
            return
        if owner != object_id:
            raise RuntimeError("approval key belongs to another object")
        c.execute(
            "UPDATE external_ids SET body_hash=? WHERE company_id=? AND namespace=? AND external_id=?",
            (body_hash, company, NS_APPROVAL, approval),
        )


def _replace_draft(
    db: Database,
    company: str,
    object_id: str,
    draft: TransactionInput,
    provenance: dict[str, str],
    author: str,
) -> None:
    values = draft.model_dump(mode="json")
    body_hash = digest(values)
    with db.transaction() as c:
        current = db.get(c, company, "transaction", object_id)
        db.replace(
            c,
            company,
            "transaction",
            object_id,
            {**values, "source_hash": body_hash, "author": author, "notion_provenance": provenance},
            current["revision"],
        )
        c.execute(
            "UPDATE external_ids SET body_hash=? WHERE company_id=? AND namespace=? AND external_id=?",
            (body_hash, company, NS_PAGE, draft.external_id),
        )


def _quarantine(db: Database, actor: Actor, company: str, item: Mapped | Hold, reason: str, edited: str, approval: str) -> dict[str, Any]:
    _upsert_quarantine(
        db,
        actor,
        company,
        item.page_id,
        {
            "page_id": item.page_id,
            "reason": reason,
            "classification": item.classification,
            "net": item.net,
            "tax": item.tax,
            "gross": item.gross,
            "approval_tail": (approval or "")[-4:],
            "last_edited_time": edited,
        },
    )
    return {
        "page_id": item.page_id,
        "reason": reason,
        "classification": item.classification,
        "gross": item.gross,
        "approval_tail": (approval or "")[-4:],
    }


def apply_drafts(
    db: Database,
    actor: Actor,
    company: str,
    report: MapReport,
    edited_by_page: Mapping[str, str],
) -> dict[str, Any]:
    """Insert or refresh drafts. Never writes kind=journal."""
    svc = Service(db)
    inserted = updated = quarantined = unchanged = provenance_writes = 0
    disappeared: list[str] = []
    quarantine_rows: list[dict[str, Any]] = []
    seen_pages: set[str] = set()

    def hold(item: Mapped | Hold, reason: str, edited: str, approval: str = "") -> None:
        nonlocal quarantined
        quarantined += 1
        quarantine_rows.append(_quarantine(db, actor, company, item, reason, edited, approval))

    for item in report.holds:
        seen_pages.add(item.page_id)
        edited = edited_by_page.get(item.page_id, "")
        hold(item, item.reason, edited, "")
        with db.connect() as c:
            existing_id = _page_object(c, company, f"notion-page:{item.page_id}")
            blocked = existing_id is not None and _journal_blocks(c, company, existing_id)
        if existing_id and not blocked:
            _stamp_provenance(db, company, existing_id, {"page_id": item.page_id, "last_edited_time": edited})

    for item in report.loaded:
        seen_pages.add(item.page_id)
        edited = edited_by_page.get(item.page_id, "")
        provenance = {"page_id": item.page_id, "last_edited_time": edited}
        approval = item.approval or ""
        with db.connect() as c:
            existing_id = _page_object(c, company, item.draft.external_id)
            owner = _approval_owner(c, company, approval) if approval else None
            stored = _stored_amounts(c, company, owner) if owner else None
            blocked = existing_id is not None and _journal_blocks(c, company, existing_id)
        incoming = (item.net, item.tax, item.gross)
        if approval and owner and stored is not None and stored != incoming:
            hold(item, "amount mismatch with existing hometax-approval key", edited, approval)
            continue
        if approval and owner and existing_id is None:
            hold(item, "hometax-approval key already used", edited, approval)
            continue
        if approval and owner and existing_id and owner != existing_id:
            hold(item, "hometax-approval key already used", edited, approval)
            continue
        if blocked:
            hold(item, "journal exists; draft not overwritten", edited, approval)
            continue
        if existing_id is None:
            created = svc.create_transaction(actor, company, item.draft)
            if created["deduplicated"]:
                raise RuntimeError("natural key missing from lookup but create deduplicated")
            inserted += 1
            body_hash = digest(item.draft.model_dump(mode="json"))
            if _stamp_provenance(db, company, created["id"], provenance):
                provenance_writes += 1
            if approval:
                _link_approval(db, company, approval, created["id"], body_hash)
            continue
        with db.connect() as c:
            current = db.get(c, company, "transaction", existing_id)
        if _same_business(current, item.draft):
            if _stamp_provenance(db, company, existing_id, provenance):
                provenance_writes += 1
            else:
                unchanged += 1
            if approval:
                _link_approval(db, company, approval, existing_id, digest(item.draft.model_dump(mode="json")))
            continue
        _replace_draft(db, company, existing_id, item.draft, provenance, current.get("author") or actor.id)
        if approval:
            _link_approval(db, company, approval, existing_id, digest(item.draft.model_dump(mode="json")))
        updated += 1

    with db.connect() as c:
        rows = c.execute(
            "SELECT external_id FROM external_ids WHERE company_id=? AND namespace=?",
            (company, NS_PAGE),
        ).fetchall()
        journals = c.execute(
            "SELECT count(*) FROM objects WHERE company_id=? AND kind='journal'",
            (company,),
        ).fetchone()[0]
    for row in rows:
        page_id = str(row["external_id"]).removeprefix("notion-page:")
        if page_id not in seen_pages:
            disappeared.append(page_id)
    return {
        "inserted": inserted,
        "updated": updated,
        "quarantined": quarantined,
        "unchanged": unchanged,
        "provenance": provenance_writes,
        "disappeared": disappeared,
        "quarantine": quarantine_rows,
        "journals": int(journals),
    }
