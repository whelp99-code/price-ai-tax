"""Atomic company-scoped persistence and append-only application audit."""
from __future__ import annotations
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def uid(prefix: str = "obj") -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


META = {"id", "revision", "created_at", "updated_at"}


def payload(record: dict) -> dict:
    return {k: v for k, v in record.items() if k not in META}


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, details: Any = None):
        super().__init__(message)
        self.code, self.message, self.status, self.details = code, message, status, details


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if tables:
                if "schema_version" not in tables:
                    raise DomainError("UNSUPPORTED_DATABASE", "price 전용 데이터베이스가 아닙니다. 기존 DB를 변경하지 않습니다.")
                version = c.execute("SELECT max(version) FROM schema_version").fetchone()[0]
                if version != 1:
                    raise DomainError("SCHEMA_VERSION", "지원하지 않는 스키마입니다. 기존 DB를 변경하지 않습니다.")
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(Path(__file__).with_name("schema.sql").read_text())
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=15000")
        try:
            yield c
        finally:
            c.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.commit()
            except BaseException:
                c.rollback()
                raise

    @staticmethod
    def unpack(row) -> dict:
        if row is None:
            raise DomainError("NOT_FOUND", "대상을 찾을 수 없습니다.", 404)
        data = json.loads(row["body"])
        return {**data, "id": row["id"], "revision": row["revision"],
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def get(self, c, company: str, kind: str, ident: str) -> dict:
        return self.unpack(c.execute("SELECT * FROM objects WHERE company_id=? AND kind=? AND id=?", (company, kind, ident)).fetchone())

    def list(self, c, company: str, kind: str) -> list[dict]:
        return [self.unpack(r) for r in c.execute("SELECT * FROM objects WHERE company_id=? AND kind=? ORDER BY created_at,id", (company, kind))]

    def create(self, c, company: str, kind: str, data: dict, ident: str | None = None) -> dict:
        ident, ts = ident or uid(kind), now()
        c.execute("INSERT INTO objects VALUES(?,?,?,?,?,?,?)", (company, kind, ident, 1, canonical(payload(data)), ts, ts))
        return self.get(c, company, kind, ident)

    def replace(self, c, company: str, kind: str, ident: str, data: dict, expected: int) -> dict:
        cur = c.execute("UPDATE objects SET body=?,revision=revision+1,updated_at=? WHERE company_id=? AND kind=? AND id=? AND revision=?", (canonical(payload(data)), now(), company, kind, ident, expected))
        if cur.rowcount != 1:
            raise DomainError("STALE_VERSION", "다른 작업이 변경했습니다. 새로고침 후 다시 확인하세요.")
        return self.get(c, company, kind, ident)

    def audit(self, c, company: str, actor: str, action: str, entity: str, detail: dict | None = None):
        row = c.execute("SELECT event_hash FROM audit WHERE company_id=? ORDER BY seq DESC LIMIT 1", (company,)).fetchone()
        previous = row[0] if row else "0" * 64
        event = dict(company_id=company, actor=actor, action=action, entity_id=entity,
                     at=now(), detail=detail or {}, previous_hash=previous)
        h = digest(event)
        c.execute("INSERT INTO audit(company_id,actor,action,entity_id,at,detail,previous_hash,event_hash) VALUES(?,?,?,?,?,?,?,?)",
                  (company, actor, action, entity, event["at"], canonical(event["detail"]), previous, h))
        return h

    def verify_audit(self, c, company: str) -> dict:
        previous, count = "0" * 64, 0
        for r in c.execute("SELECT * FROM audit WHERE company_id=? ORDER BY seq", (company,)):
            event = {k: r[k] for k in ("company_id", "actor", "action", "entity_id", "at", "previous_hash")}
            event["detail"] = json.loads(r["detail"])
            if r["previous_hash"] != previous or digest(event) != r["event_hash"]:
                return {"valid": False, "events": count, "failed_sequence": r["seq"]}
            previous, count = r["event_hash"], count + 1
        return {"valid": True, "events": count, "head": previous, "external_anchor": False}

    def backup(self, target: Path) -> dict:
        target = target.expanduser().resolve()
        if target.exists() or target == self.path:
            raise DomainError("TARGET_EXISTS", "기존 파일은 덮어쓰지 않습니다.")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with target.open("xb"):
            pass
        try:
            with self.connect() as src:
                dst = sqlite3.connect(target)
                try:
                    src.backup(dst)
                    check = dst.execute("PRAGMA integrity_check").fetchone()[0]
                finally:
                    dst.close()
            os.chmod(target, 0o600)
            return {"path": str(target), "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "integrity": check}
        except BaseException:
            target.unlink(missing_ok=True)
            raise
