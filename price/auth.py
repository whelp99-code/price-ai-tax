from __future__ import annotations
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from .db import Database, DomainError, canonical, uid, now
from .models import CompanyInput, UserInput

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
        self.db = db

    def guard(self, c, actor: Actor, company: str, allowed=("owner", "preparer", "reviewer", "viewer")) -> str:
        if actor.worker:
            if allowed == ("worker",):
                return "worker"
            raise DomainError("WORKER_FORBIDDEN", "worker는 승인·확정할 수 없습니다.", 403)
        row = c.execute("SELECT m.role FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.company_id=? AND m.user_id=? AND u.disabled=0", (company, actor.id)).fetchone()
        if not row:
            raise DomainError("NOT_FOUND", "회사 또는 대상에 접근할 수 없습니다.", 404)
        if row[0] not in allowed:
            raise DomainError("FORBIDDEN", "이 작업의 권한이 없습니다.", 403)
        return row[0]

    def add_user(self, c, company: str, data: UserInput) -> dict:
        if c.execute("SELECT 1 FROM users WHERE username=?", (data.username,)).fetchone():
            raise DomainError("USERNAME_EXISTS", "이미 존재하는 사용자입니다.")
        ident, salt = uid("user"), secrets.token_hex(16)
        c.execute("INSERT INTO users(id,username,salt,password_hash) VALUES(?,?,?,?)", (ident, data.username, salt, password_hash(data.password, salt)))
        c.execute("INSERT INTO memberships VALUES(?,?,?)", (ident, company, data.role))
        return {"id": ident, "username": data.username, "role": data.role}

    def bootstrap(self, profile: CompanyInput, username: str, password: str, demo: bool = False) -> tuple[str, Actor]:
        data = UserInput(username=username, password=password, role="owner")
        with self.db.transaction() as c:
            if c.execute("SELECT 1 FROM companies LIMIT 1").fetchone():
                raise DomainError("ALREADY_INITIALIZED", "기존 데이터베이스를 초기화하거나 덮어쓰지 않습니다.")
            company = uid("company")
            c.execute("INSERT INTO companies VALUES(?,?,?)", (company, canonical({**profile.model_dump(), "demo": demo, "ledger_mode": "validation"}), now()))
            user = self.add_user(c, company, data)
            self.db.audit(c, company, user["id"], "company.initialized", company, {"demo": demo})
            return company, Actor(user["id"], user["username"])

    def login(self, username: str, password: str) -> tuple[str, Actor]:
        failure = None
        with self.db.transaction() as c:
            row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            ts = int(time.time())
            if not row:
                password_hash(password, "00" * 16)
                failure = DomainError("INVALID_LOGIN", "아이디 또는 비밀번호를 확인하세요.", 401)
            elif row["disabled"] or row["locked_until"] > ts:
                failure = DomainError("LOGIN_LOCKED", "잠시 후 다시 시도하세요.", 429)
            elif not hmac.compare_digest(password_hash(password, row["salt"]), row["password_hash"]):
                count = row["failures"] + 1
                c.execute("UPDATE users SET failures=?,locked_until=? WHERE id=?", (count, ts + 900 if count >= 10 else 0, row["id"]))
                failure = DomainError("INVALID_LOGIN", "아이디 또는 비밀번호를 확인하세요.", 401)
            else:
                c.execute("UPDATE users SET failures=0,locked_until=0 WHERE id=?", (row["id"],))
                c.execute("DELETE FROM sessions WHERE expires_at<=?", (ts,))
                token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
                c.execute("INSERT INTO sessions VALUES(?,?,?,?)", (token_hash(token), row["id"], csrf, ts + SESSION_SECONDS))
                actor = Actor(row["id"], row["username"], csrf)
        if failure:
            raise failure
        return token, actor

    def session(self, token: str | None) -> Actor:
        if not token or len(token) > 200:
            raise DomainError("UNAUTHENTICATED", "로그인이 필요합니다.", 401)
        with self.db.connect() as c:
            row = c.execute("SELECT u.id,u.username,s.csrf FROM sessions s JOIN users u ON s.user_id=u.id WHERE s.token_hash=? AND s.expires_at>? AND u.disabled=0", (token_hash(token), int(time.time()))).fetchone()
            if not row:
                raise DomainError("UNAUTHENTICATED", "세션이 만료되었습니다.", 401)
            return Actor(row["id"], row["username"], row["csrf"])

    def logout(self, token: str):
        with self.db.transaction() as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))

    def companies(self, actor: Actor) -> list[dict]:
        import json
        with self.db.connect() as c:
            return [{**json.loads(r["body"]), "id": r["id"], "role": r["role"]} for r in c.execute("SELECT co.*,m.role FROM companies co JOIN memberships m ON m.company_id=co.id WHERE m.user_id=?", (actor.id,))]
