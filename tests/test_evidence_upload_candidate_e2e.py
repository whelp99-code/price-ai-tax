"""Loopback E2E: upload evidence, link it to one unposted transaction, reread it.

Uses a temporary database and an ephemeral 127.0.0.1 port. It refuses the
production listener on 4382 and does not create a journal.
"""
from __future__ import annotations

import base64
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from price.db import Database
from price.models import CompanyInput
from price.service import Service

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "Synthetic-Evidence-E2E-2026!"
PRODUCTION_PORT = 4382
RAW = b"SYNTHETIC EVIDENCE UPLOAD - NOT REAL TAX DATA\n"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    if port == PRODUCTION_PORT:
        raise RuntimeError("ephemeral port collided with production 4382")
    return port


def _wait_health(process: subprocess.Popen[bytes], client: httpx.Client) -> None:
    for _ in range(120):
        if process.poll() is not None:
            raise RuntimeError(f"dev server exited: {process.returncode}")
        try:
            if client.get("/health").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError("dev server health timed out")


def test_upload_links_to_unposted_transaction_and_reread_shows_ref():
    with tempfile.TemporaryDirectory(prefix="price-evidence-e2e-") as tmp:
        db_path = Path(tmp) / "price.sqlite3"
        company, _owner = Service(Database(db_path)).auth.bootstrap(
            CompanyInput(name="증빙 후보 시험 법인"), "evidence-owner", PASSWORD
        )
        port = _free_port()
        url = f"http://127.0.0.1:{port}"
        log_path = Path(tmp) / "server.log"
        with log_path.open("w") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "price", "--db", str(db_path), "serve", "--host", "127.0.0.1", "--port", str(port)],
                cwd=ROOT,
                stdout=log,
                stderr=log,
            )
            try:
                with httpx.Client(base_url=url, timeout=10, trust_env=False) as client:
                    _wait_health(process, client)
                    prefix = f"/api/companies/{company}"
                    login = client.post("/api/login", json={"username": "evidence-owner", "password": PASSWORD})
                    assert login.status_code == 200, login.text
                    client.headers["x-csrf-token"] = login.json()["csrf"]

                    uploaded = client.post(
                        prefix + "/evidence",
                        json={"filename": "synthetic-evidence.txt", "content_base64": base64.b64encode(RAW).decode()},
                    )
                    assert uploaded.status_code == 201, uploaded.text
                    evidence_id = uploaded.json()["id"]
                    assert evidence_id.startswith("evidence")

                    created = client.post(
                        prefix + "/transactions",
                        json={
                            "external_id": "evidence-candidate-1",
                            "direction": "purchase",
                            "event_date": "2026-10-31",
                            "counterparty": "합성 거래처",
                            "net": 1000,
                            "tax": 100,
                            "gross": 1100,
                            "tax_category": "standard",
                            "business_purpose": "증빙 연결 전 후보",
                            "evidence_ids": [],
                        },
                    )
                    assert created.status_code == 201, created.text
                    transaction_id = created.json()["id"]
                    assert created.json()["evidence_ids"] == []

                    linked = client.post(
                        prefix + f"/transactions/{transaction_id}/facts",
                        json={
                            "expected_revision": created.json()["revision"],
                            "business_purpose": "업로드 증빙을 후보 거래에 연결",
                            "tax_category": "standard",
                            "deductible": True,
                            "evidence_ids": [evidence_id],
                            "reason": "개발 E2E 후보 연결",
                        },
                    )
                    assert linked.status_code == 200, linked.text
                    assert linked.json()["evidence_ids"] == [evidence_id]

                    reread = client.get(prefix + f"/transactions/{transaction_id}")
                    assert reread.status_code == 200, reread.text
                    assert reread.json()["id"] == transaction_id
                    assert reread.json()["evidence_ids"] == [evidence_id]
                    assert client.get(prefix + "/journals").json() == []
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
