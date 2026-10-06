import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from price.db import Database, DomainError, canonical, now, uid
from price.models import CompanyInput, UserInput
from price.review_cases import ReviewCases, ReviewRequest
from price.service import Service

PASSWORD = "Test-Only-Price-2026!"


class World:
    def __init__(self, db, svc, company, owner):
        self.db, self.svc, self.company, self.owner = db, svc, company, owner


def reset_changed(db_path: str, company: str, ident: str) -> None:
    db = Database(db_path)
    with db.transaction() as c:
        row = db.get(c, company, "expert_review", ident)
        if row.get("state") != "SOURCE_INVALIDATED":
            return
        db.replace(c, company, "expert_review", ident, {**row, "state": "UNASSIGNED", "source_invalidated": False}, row["revision"])


def main() -> None:
    if len(sys.argv) == 5 and sys.argv[1] == "reset":
        reset_changed(sys.argv[2], sys.argv[3], sys.argv[4])
        return
    if len(sys.argv) != 3:
        raise SystemExit("usage: seed_discrepancy_qa.py DB JSON")
    casework = importlib.import_module("test_casework")
    db = Database(sys.argv[1])
    svc = Service(db)
    company, owner = svc.auth.bootstrap(CompanyInput(name="합성 회사 A"), "qa-owner", PASSWORD)
    viewer = svc.add_user(owner, company, UserInput(username="qa-viewer", password=PASSWORD, role="viewer"))
    other = uid("company")
    with db.transaction() as c:
        c.execute(
            "INSERT INTO companies VALUES(?,?,?)",
            (other, canonical({"name": "합성 회사 B", "demo": False, "ledger_mode": "validation"}), now()),
        )
        foreign = svc.auth.add_user(c, other, UserInput(username="qa-foreign", password=PASSWORD, role="owner"))
        db.audit(c, other, foreign["id"], "company.initialized", other)
    world = World(db, svc, company, owner)
    work = casework.setup_sources(world)
    events = work.list(owner, company)
    changed_event = casework.invoice(work, world)
    equal_event = next(
        event for event in events
        if event["id"] != changed_event["id"]
        and not set(event["source_record_ids"]) & set(changed_event["source_record_ids"])
    )
    reviews = ReviewCases(svc)
    changed = reviews.open(owner, company, ReviewRequest(
        event_id=changed_event["id"], reason="Changed marker", idempotency_key="qa-changed-marker01"))
    equal = reviews.open(owner, company, ReviewRequest(
        event_id=equal_event["id"], reason="Equal marker", idempotency_key="qa-equal-marker001"))
    with db.transaction() as c:
        source = db.get(c, company, "source_record", changed_event["source_record_ids"][0])
        db.replace(c, company, "source_record", source["id"], {**source, "lifecycle": "SUPERSEDED"}, source["revision"])
        foreign_review = db.create(c, other, "expert_review", {
            "event_id": "foreign-synthetic-event",
            "reason": "Foreign marker",
            "state": "UNASSIGNED",
            "source_context_hash": "a" * 64,
            "fact_context_hash": "b" * 64,
            "professional_credential_verified": False,
            "requester": foreign["id"],
        })
    with db.connect() as c:
        try:
            reviews._fresh(c, company, changed["id"], changed["revision"])
        except DomainError as error:
            if error.code not in {"SOURCE_CHANGED", "REVIEW_SOURCE_CHANGED"}:
                raise
        else:
            raise SystemExit("changed review was still fresh")
        reviews._fresh(c, company, equal["id"], equal["revision"])
    Path(sys.argv[2]).write_text(json.dumps({
        "owner": "qa-owner",
        "viewer": "qa-viewer",
        "foreign": "qa-foreign",
        "viewer_id": viewer["id"],
        "foreign_id": foreign["id"],
        "company": company,
        "other_company": other,
        "changed_review": changed["id"],
        "equal_review": equal["id"],
        "foreign_review": foreign_review["id"],
        "password": PASSWORD,
    }))


if __name__ == "__main__":
    main()
