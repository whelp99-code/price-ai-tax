from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from dataclasses import dataclass

from pydantic import TypeAdapter

from .db import Database, DomainError, canonical, now, uid
from .models import CompanyInput, UserInput
from .sql_types import read_row, row_value

ITERATIONS = 600_000
SESSION_SECONDS = 8 * 3600


def password_hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS).hex()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Actor:
    id: str
    username: str
    csrf: str = ""
    worker: bool = False


class Auth:
    def __init__(self, db: Database):
        self.db: Database = db

    def guard(self, c: sqlite3.Connection, actor: Actor, company: str,
              allowed: tuple[str, ...] = ("owner", "preparer", "reviewer", "viewer")) -> str:
        if actor.worker:
            if allowed == ("worker",):
                return "worker"
            raise DomainError("WORKER_FORBIDDEN", "worker는 승인·확정할 수 없습니다.", 403)
        row = read_row(c.execute("SELECT m.role FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.company_id=? AND m.user_id=? AND u.disabled=0", (company, actor.id)))
        if not row:
            raise DomainError("NOT_FOUND", "회사 또는 대상에 접근할 수 없습니다.", 404)
        if row[0] not in allowed:
            raise DomainError("FORBIDDEN", "이 작업의 권한이 없습니다.", 403)
        role = row_value(row, 0, str)
        return role

    def add_user(self, c: sqlite3.Connection, company: str, data: UserInput) -> dict[str, str]:
        if c.execute("SELECT 1 FROM users WHERE username=?", (data.username,)).fetchone():
            raise DomainError("USERNAME_EXISTS", "이미 존재하는 사용자입니다.")
        ident, salt = uid("user"), secrets.token_hex(16)
        _ = c.execute("INSERT INTO users(id,username,salt,password_hash) VALUES(?,?,?,?)", (ident, data.username, salt, password_hash(data.password, salt)))
        _ = c.execute("INSERT INTO memberships VALUES(?,?,?)", (ident, company, data.role))
        return {"id": ident, "username": data.username, "role": data.role}

    def bootstrap(self, profile: CompanyInput, username: str, password: str, demo: bool = False) -> tuple[str, Actor]:
        data = UserInput(username=username, password=password, role="owner")
        with self.db.transaction() as c:
            if c.execute("SELECT 1 FROM companies LIMIT 1").fetchone():
                raise DomainError("ALREADY_INITIALIZED", "기존 데이터베이스를 초기화하거나 덮어쓰지 않습니다.")
            company = uid("company")
            _ = c.execute("INSERT INTO companies VALUES(?,?,?)", (company, canonical({**profile.model_dump(), "demo": demo, "ledger_mode": "validation"}), now()))
            user = self.add_user(c, company, data)
            _ = self.db.audit(c, company, user["id"], "company.initialized", company, {"demo": demo})
            return company, Actor(user["id"], user["username"])

    def login(self, username: str, password: str) -> tuple[str, Actor]:
        failure = None
        authenticated: tuple[str, Actor] | None = None
        with self.db.transaction() as c:
            row = read_row(c.execute("SELECT * FROM users WHERE username=?", (username,)))
            ts = int(time.time())
            if not row:
                _ = password_hash(password, "00" * 16)
                failure = DomainError("INVALID_LOGIN", "아이디 또는 비밀번호를 확인하세요.", 401)
            elif row_value(row, "disabled", int) or row_value(row, "locked_until", int) > ts:
                failure = DomainError("LOGIN_LOCKED", "잠시 후 다시 시도하세요.", 429)
            elif not hmac.compare_digest(password_hash(password, row_value(row, "salt", str)), row_value(row, "password_hash", str)):
                count = row_value(row, "failures", int) + 1
                _ = c.execute("UPDATE users SET failures=?,locked_until=? WHERE id=?", (count, ts + 900 if count >= 10 else 0, row["id"]))
                failure = DomainError("INVALID_LOGIN", "아이디 또는 비밀번호를 확인하세요.", 401)
            else:
                _ = c.execute("UPDATE users SET failures=0,locked_until=0 WHERE id=?", (row["id"],))
                _ = c.execute("DELETE FROM sessions WHERE expires_at<=?", (ts,))
                token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
                _ = c.execute("INSERT INTO sessions VALUES(?,?,?,?)", (token_hash(token), row["id"], csrf, ts + SESSION_SECONDS))
                user_id = row_value(row, "id", str)
                stored_username = row_value(row, "username", str)
                actor = Actor(user_id, stored_username, csrf)
                authenticated = token, actor
        if failure:
            raise failure
        assert authenticated is not None
        return authenticated

    def session(self, token: str | None) -> Actor:
        if not token or len(token) > 200:
            raise DomainError("UNAUTHENTICATED", "로그인이 필요합니다.", 401)
        with self.db.connect() as c:
            row = read_row(c.execute("SELECT u.id,u.username,s.csrf FROM sessions s JOIN users u ON s.user_id=u.id WHERE s.token_hash=? AND s.expires_at>? AND u.disabled=0", (token_hash(token), int(time.time()))))
            if not row:
                raise DomainError("UNAUTHENTICATED", "세션이 만료되었습니다.", 401)
            user_id = row_value(row, "id", str)
            username = row_value(row, "username", str)
            csrf = row_value(row, "csrf", str)
            return Actor(user_id, username, csrf)

    def logout(self, token: str):
        with self.db.transaction() as c:
            _ = c.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))

    def companies(self, actor: Actor) -> list[dict[str, object]]:
        with self.db.connect() as c:
            rows: list[sqlite3.Row] = c.execute("SELECT co.*,m.role FROM companies co JOIN memberships m ON m.company_id=co.id WHERE m.user_id=?", (actor.id,)).fetchall()
            companies: list[dict[str, object]] = []
            for row in rows:
                body = row_value(row, "body", str)
                profile = TypeAdapter(dict[str, object]).validate_json(body)
                profile["id"] = row_value(row, "id", str)
                profile["role"] = row_value(row, "role", str)
                companies.append(profile)
            return companies

    def disable_user(self, actor: Actor, company: str, user_id: str) -> dict[str, str | bool | int]:
        with self.db.transaction() as c:
            _ = self.guard(c, actor, company, ("owner",))
            memberships: list[sqlite3.Row] = c.execute(
                "SELECT company_id FROM memberships WHERE user_id=? ORDER BY company_id",
                (user_id,),
            ).fetchall()
            company_ids: list[str] = [row["company_id"] for row in memberships]
            if company not in company_ids:
                raise DomainError("TENANT_MISMATCH", "User does not belong to this company.", 403)
            # Account disable affects every membership, not just the requested company.
            for company_id in company_ids:
                _ = self.guard(c, actor, company_id, ("owner",))
            _ = c.execute("UPDATE users SET disabled=1 WHERE id=?", (user_id,))
            revoked = c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,)).rowcount
            for company_id in company_ids:
                _ = self.db.audit(c, company_id, actor.id, "member.disabled", user_id,
                              {"sessions_revoked": revoked})
            return {"id": user_id, "disabled": True, "sessions_revoked": revoked}
