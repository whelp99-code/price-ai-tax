from __future__ import annotations
import argparse
import getpass
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from .db import Database, DomainError
from .service import Service
from .models import CompanyInput


def main():
    parser = argparse.ArgumentParser(description="price — standalone AI tax-agent validation release")
    parser.add_argument("--db", default=os.getenv("PRICE_DB", ".data/price.sqlite3"))
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init"); init.add_argument("--demo", action="store_true"); init.add_argument("--company", default="내 회사"); init.add_argument("--username", default="owner")
    serve = sub.add_parser("serve"); serve.add_argument("--host", default="127.0.0.1"); serve.add_argument("--port", type=int, default=int(os.getenv("PRICE_PORT", "4381")))
    backup = sub.add_parser("backup"); backup.add_argument("target")
    restore = sub.add_parser("restore"); restore.add_argument("source"); restore.add_argument("target"); restore.add_argument("--sha256", required=True)
    sub.add_parser("status")
    args = parser.parse_args()
    try:
        if args.command == "restore":
            source, target = Path(args.source).resolve(), Path(args.target).resolve()
            if target.exists(): raise DomainError("TARGET_EXISTS", "복원 대상이 이미 존재합니다.")
            if hashlib.sha256(source.read_bytes()).hexdigest() != args.sha256:
                raise DomainError("BACKUP_HASH_MISMATCH", "백업 해시가 일치하지 않습니다.")
            src = sqlite3.connect(source.as_uri()+"?mode=ro", uri=True)
            try:
                if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok": raise DomainError("BACKUP_INVALID", "백업 무결성 오류")
                if src.execute("SELECT max(version) FROM schema_version").fetchone()[0] != 1: raise DomainError("SCHEMA_VERSION", "지원하지 않는 스키마")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with target.open("xb"): pass
                dst = sqlite3.connect(target)
                try:
                    src.backup(dst)
                    dst.execute("DELETE FROM sessions")
                    dst.execute("UPDATE jobs SET state='QUEUED',lease_until=0 WHERE state='RUNNING'")
                    dst.commit()
                finally: dst.close()
            finally: src.close()
            os.chmod(target, 0o600)
            print(json.dumps({"restored": str(target), "sessions_revoked": True, "external_submissions_replayed": False})); return
        db = Database(args.db)
        svc = Service(db)
        if args.command == "init":
            if args.demo:
                from .demo import seed_demo
                print(json.dumps(seed_demo(svc), ensure_ascii=False, indent=2))
            else:
                password = getpass.getpass("초기 owner 비밀번호(12자 이상): ")
                if getpass.getpass("비밀번호 확인: ") != password: raise DomainError("PASSWORD_MISMATCH", "비밀번호가 다릅니다.")
                company, actor = svc.auth.bootstrap(CompanyInput(name=args.company), args.username, password)
                print(json.dumps({"company_id": company, "username": actor.username, "demo": False}, ensure_ascii=False))
        elif args.command == "backup":
            print(json.dumps(db.backup(Path(args.target)), ensure_ascii=False, indent=2))
        elif args.command == "status":
            with db.connect() as c:
                print(json.dumps({"db": str(db.path), "project": "price", "worker": "price", "companies": c.execute("SELECT count(*) FROM companies").fetchone()[0], "objects": c.execute("SELECT count(*) FROM objects").fetchone()[0], "integrity": c.execute("PRAGMA integrity_check").fetchone()[0], "live_filing": False}, ensure_ascii=False, indent=2))
        elif args.command == "serve":
            with db.connect() as c:
                rows = c.execute("SELECT body FROM companies").fetchall()
                if not rows: raise DomainError("NOT_INITIALIZED", "먼저 init 명령으로 초기화하세요.")
                demo = any(json.loads(r[0]).get("demo") for r in rows)
            if args.host not in ("127.0.0.1", "localhost", "::1"):
                if demo or os.getenv("PRICE_ALLOW_PUBLIC_BIND") != "1" or os.getenv("PRICE_SECURE_COOKIE") != "1":
                    raise DomainError("PUBLIC_BIND_BLOCKED", "데모 공개 노출은 금지됩니다. 운영 공개 배치는 TLS·보안 검토가 필요합니다.")
            import uvicorn
            from .api import create_app
            uvicorn.run(create_app(args.db), host=args.host, port=args.port, access_log=False)
    except (DomainError, ValueError) as exc:
        print(json.dumps({"error": getattr(exc, "code", "INVALID_INPUT"), "message": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == "__main__": main()
