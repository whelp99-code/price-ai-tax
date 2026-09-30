"""Durable proposal worker; no posting, approval or external submission capability."""
from __future__ import annotations
import argparse
import json
import os
import time
from .auth import Actor
from .db import Database, DomainError, canonical, now, uid
from .service import Service, WRITERS
from .ingestion import Ingestion
from .casework import Casework
from .event_settlements import EventSettlements


class Queue:
    def __init__(self, service: Service):
        self.service, self.db, self.auth = service, service.db, service.auth

    def enqueue(self, actor, company, ident):
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, WRITERS)
            tx = self.db.get(c, company, "transaction", ident)
            row = c.execute("SELECT * FROM jobs WHERE company_id=? AND transaction_id=? AND input_revision=?", (company, ident, tx["revision"])).fetchone()
            if row: return dict(row)
            job = uid("job")
            c.execute("INSERT INTO jobs(id,company_id,transaction_id,input_revision,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (job, company, ident, tx["revision"], "QUEUED", now(), now()))
            self.db.audit(c, company, actor.id, "job.enqueued", job)
            return dict(c.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone())

    def list(self, actor, company):
        with self.db.connect() as c:
            self.auth.guard(c, actor, company)
            return [dict(r) for r in c.execute("SELECT * FROM jobs WHERE company_id=? ORDER BY created_at DESC LIMIT 200", (company,))]

    def run_once(self, company: str | None = None):
        with self.db.transaction() as c:
            ts = int(time.time())
            c.execute("UPDATE jobs SET state='FAILED',error='LEASE_RETRY_EXHAUSTED',lease_until=0,updated_at=? WHERE state='RUNNING' AND lease_until<? AND attempts>=3" + (" AND company_id=?" if company else ""), [now(), ts] + ([company] if company else []))
            params = [ts]
            sql = "SELECT * FROM jobs WHERE (state='QUEUED' OR (state='RUNNING' AND lease_until<?)) AND attempts<3"
            if company:
                sql += " AND company_id=?"; params.append(company)
            row = c.execute(sql + " ORDER BY created_at LIMIT 1", params).fetchone()
            if not row: return {"processed": False}
            job, lease, fence = dict(row), ts + 60, now()
            c.execute("UPDATE jobs SET state='RUNNING',attempts=attempts+1,lease_until=?,updated_at=? WHERE id=?", (lease, fence, job["id"]))
            tx = self.db.get(c, job["company_id"], "transaction", job["transaction_id"])
        actor = Actor("system:price", "price", worker=True)
        try:
            if tx["revision"] != job["input_revision"]:
                raise DomainError("STALE_JOB", "작업 등록 후 거래가 변경되었습니다.")
            result = self.service.analyze(actor, job["company_id"], job["transaction_id"], expected_revision=job["input_revision"])
            state, error = ("SUCCEEDED" if result["case"]["state"] in ("PROPOSED", "CLOSED") else "BLOCKED"), None
        except DomainError as exc:
            result, state, error = None, "BLOCKED", exc.code
        except Exception:
            result, state, error = None, "FAILED", "WORKER_INTERNAL_ERROR"
        with self.db.transaction() as c:
            changed = c.execute("UPDATE jobs SET state=?,result=?,error=?,lease_until=0,updated_at=? WHERE id=? AND state='RUNNING' AND updated_at=?", (state, canonical(result) if result else None, error, now(), job["id"], fence)).rowcount
            if not changed:
                return {"processed": True, "id": job["id"], "state": "LEASE_LOST_RESULT_NOT_COMMITTED"}
            self.db.audit(c, job["company_id"], actor.id, "job.finished", job["id"], {"state": state, "error": error})
        return {"processed": True, "id": job["id"], "state": state, "result": result, "error": error}


def main():
    parser = argparse.ArgumentParser(description="price proposal worker")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--db", default=os.getenv("PRICE_DB", ".data/price.sqlite3"))
    args = parser.parse_args()
    service = Service(Database(args.db))
    q, reader = Queue(service), Ingestion(service)
    casework = Casework(service)
    settlements = EventSettlements(service)
    from .opening_books import OpeningBooks
    openings=OpeningBooks(service)
    from .internal_transfers import InternalTransfers
    from .card_books import CardBooks
    transfers,cards=InternalTransfers(service),CardBooks(service)
    from .pipeline_health import WorkerHeartbeat
    heartbeat = WorkerHeartbeat(service.db)
    heartbeat.record(force=True)
    turn = 0
    while True:
        heartbeat.record()
        primary, secondary = (reader, q) if turn % 2 == 0 else (q, reader)
        turn += 1
        result = primary.run_once()
        if not result["processed"]:
            result = secondary.run_once()
        if not result["processed"]:
            result = casework.run_once()
        if not result["processed"]:
            result = settlements.run_once()
        if not result['processed']:
            result = openings.run_once()
        if not result['processed']:
            result = transfers.run_once()
        if not result['processed']:
            result = cards.run_once()
        if result["processed"] or args.once:
            summary = {k: result[k] for k in ("processed", "id", "state", "error") if k in result}
            print(json.dumps(summary, ensure_ascii=False), flush=True)
        heartbeat.record(result)
        if args.once:
            heartbeat.stopped()
            break
        time.sleep(2 if not result["processed"] else 0.1)


if __name__ == "__main__":
    main()
