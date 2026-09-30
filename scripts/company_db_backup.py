#!/usr/bin/env python3
"""Nightly sqlite backup of the company DB. Does not touch the demo DB."""
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path("/home/jm/orca/projects/price")
DB = Path(os.environ.get("PRICE_COMPANY_DB", "/home/jm/.local/share/price/company.sqlite3"))
DEST = Path(os.environ.get("PRICE_COMPANY_BACKUP_DIR", "/home/jm/.local/share/price/backups"))
KEEP = 14


def main() -> int:
    if not DB.is_file():
        print("company db missing", file=sys.stderr)
        return 1
    DEST.mkdir(parents=True, exist_ok=True)
    os.chmod(DEST, 0o700)
    stamp = subprocess.check_output(["date", "-u", "+%Y%m%dT%H%M%SZ"], text=True).strip()
    target = DEST / f"company-{stamp}.sqlite3"
    subprocess.check_call(
        [sys.executable, "-m", "price", "--db", str(DB), "backup", str(target)],
        cwd=ROOT,
    )
    os.chmod(target, 0o600)
    files = sorted(DEST.glob("company-*.sqlite3"))
    for old in files[:-KEEP]:
        old.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
