#!/usr/bin/env python3
"""Apply a Notion finance snapshot onto a price DB as drafts only.

Backup uses sqlite3.Connection.backup. Does not open the demo database.
Does not create journals or call the network.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from price.auth import Actor
from price.db import Database
import importlib.util

from price.notion_finance_ingest import apply_drafts, edited_index
from price.notion_finance_map import map_snapshot

_s2 = Path(__file__).with_name("notion_finance_s2.py")
_spec = importlib.util.spec_from_file_location("notion_finance_s2", _s2)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
read_hometax_purchases = _mod.read_hometax_purchases
read_hometax_purchase_sources = _mod.read_hometax_purchase_sources

DEMO = Path("/home/jm/orca/projects/price/.data/demo.sqlite3").resolve()


def refuse_demo(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved == DEMO or resolved.name == "demo.sqlite3":
        raise SystemExit("refusing demo database")
    return resolved


def backup_sqlite(source: Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"company-pre-ingest-{stamp}.sqlite3"
    if target.exists():
        raise SystemExit("backup target exists")
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        check = dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst.close()
        src.close()
    if check != "ok":
        target.unlink(missing_ok=True)
        raise SystemExit("backup integrity failed")
    os.chmod(target, 0o600)
    return target


def owner_actor(db: Database) -> tuple[str, Actor]:
    with db.connect() as c:
        rows = c.execute(
            """SELECT u.id, u.username, m.company_id
               FROM memberships m JOIN users u ON u.id=m.user_id
               WHERE m.role='owner' AND u.disabled=0"""
        ).fetchall()
    if len(rows) != 1:
        raise SystemExit("expected exactly one owner membership")
    return rows[0]["company_id"], Actor(rows[0]["id"], rows[0]["username"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--hometax-dir", type=Path)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    db_path = refuse_demo(args.db)
    backup_path = None
    if args.backup_dir:
        backup_path = backup_sqlite(db_path, args.backup_dir)
    payload = json.loads(args.snapshot.read_text())
    finance = payload["finance"]
    if args.hometax_dir:
        hometax, separate = read_hometax_purchase_sources(args.hometax_dir)
    else:
        hometax, separate = {}, []
    mapped = map_snapshot(finance, hometax)
    db = Database(db_path)
    with db.connect() as c:
        journals_before = c.execute("SELECT count(*) FROM objects WHERE kind='journal'").fetchone()[0]
    company, actor = owner_actor(db)
    result = apply_drafts(db, actor, company, mapped, edited_index(finance))
    result["backup"] = str(backup_path) if backup_path else None
    result["journals_before"] = int(journals_before)
    result["hometax_numeric"] = len(hometax)
    result["hometax_separate_non_digit"] = len(separate)
    result["hometax_matched"] = len(hometax)
    result["hometax_separate"] = [
        {"approval_tail": item.get("approval_tail", ""), "gross": item.get("gross", ""), "reason": item.get("reason", "")}
        for item in separate
    ]
    result["hometax_separate_gross"] = sum(int(item.get("gross") or 0) for item in separate)
    result["hometax_all_row_gross"] = sum(item[2] for item in hometax.values()) + result["hometax_separate_gross"]
    result["core_ingest_reads_hometax"] = False
    if result["journals"] != int(journals_before):
        raise SystemExit("draft ingest created a journal")
    text = json.dumps(result, ensure_ascii=False)
    print(text)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n")
        os.chmod(args.report, 0o600)


if __name__ == "__main__":
    main()
