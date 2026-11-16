"""Loopback E2E for the evidence candidate link API.

Uses a temporary database and an ephemeral 127.0.0.1 port. It refuses the
production listener on 4382 and does not post a journal.
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
import pytest

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


@pytest.fixture
def dev_price():
    with tempfile.TemporaryDirectory(prefix="price-evidence-e2e-") as tmp:
        db_path = Path(tmp) / "price.sqlite3"
        company, _owner = Service(Database(db_path)).auth.bootstrap(
            CompanyInput(name="증빙 후보 시험 법인"), "evidence-owner", PASSWORD
        )
        port = _free_port()
        log_path = Path(tmp) / "server.log"
        with log_path.open("w") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "price", "--db", str(db_path), "serve", "--host", "127.0.0.1", "--port", str(port)],
                cwd=ROOT,
                stdout=log,
                stderr=log,
            )
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False) as client:
                    _wait_health(process, client)
                    login = client.post("/api/login", json={"username": "evidence-owner", "password": PASSWORD})
                    assert login.status_code == 200, login.text
                    client.headers["x-csrf-token"] = login.json()["csrf"]
                    yield client, f"/api/companies/{company}"
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


def _upload(client, prefix, name):
    uploaded = client.post(prefix + "/evidence", json={"filename": name, "content_base64": base64.b64encode(RAW).decode()})
    assert uploaded.status_code == 201, uploaded.text
    return uploaded.json()["id"]


def _draft(client, prefix, external_id):
    created = client.post(prefix + "/transactions", json={
        "external_id": external_id, "direction": "purchase", "event_date": "2026-10-31",
        "counterparty": "합성 거래처", "net": 1000, "tax": 100, "gross": 1100,
        "tax_category": "standard", "business_purpose": "증빙 연결 전 후보", "evidence_ids": [],
    })
    assert created.status_code == 201, created.text
    return created.json()


def test_candidate_link_reread_unlink(dev_price):
    client, prefix = dev_price
    evidence_id = _upload(client, prefix, "synthetic-evidence.txt")
    created = _draft(client, prefix, "evidence-candidate-1")
    money = {key: created[key] for key in ("net", "tax", "gross")}

    rejected = client.post(prefix + f"/transactions/{created['id']}/evidence-links", json={
        "expected_revision": created["revision"], "evidence_id": evidence_id, "status": "confirmed", "reason": "확정을 한 번에 넣지 못한다",
    })
    assert rejected.status_code == 422

    linked = client.post(prefix + f"/transactions/{created['id']}/evidence-links", json={
        "expected_revision": created["revision"], "evidence_id": evidence_id, "reason": "조건이 맞아 후보로만 연결",
    })
    assert linked.status_code == 200, linked.text
    assert linked.json()["evidence_ids"] == []
    assert {key: linked.json()[key] for key in money} == money

    reread = client.get(prefix + f"/transactions/{created['id']}")
    assert reread.status_code == 200, reread.text
    body = reread.json()
    assert body["evidence_links"] == [{"evidence_id": evidence_id, "status": "candidate", "reason": "조건이 맞아 후보로만 연결"}]
    assert body["evidence_ids"] == []
    assert client.get(prefix + "/journals").json() == []

    unlinked = client.post(prefix + f"/transactions/{created['id']}/evidence-links/{evidence_id}/unlink", json={
        "expected_revision": body["revision"], "reason": "후보 해제",
    })
    assert unlinked.status_code == 200, unlinked.text
    assert unlinked.json()["evidence_links"] == []
    assert {key: unlinked.json()[key] for key in money} == money

    after = client.get(prefix + f"/transactions/{created['id']}")
    assert after.status_code == 200, after.text
    assert after.json()["evidence_links"] == []
    assert after.json()["evidence_ids"] == []
    assert client.get(prefix + "/journals").json() == []


def test_confirm_is_separate_and_does_not_change_books(dev_price):
    client, prefix = dev_price
    evidence_id = _upload(client, prefix, "synthetic-confirm.txt")
    created = _draft(client, prefix, "evidence-confirm-1")
    money = {key: created[key] for key in ("net", "tax", "gross")}
    linked = client.post(prefix + f"/transactions/{created['id']}/evidence-links", json={
        "expected_revision": created["revision"], "evidence_id": evidence_id, "reason": "후보 메모",
    })
    assert linked.status_code == 200, linked.text
    confirmed = client.post(prefix + f"/transactions/{created['id']}/evidence-links/{evidence_id}/confirm", json={
        "expected_revision": linked.json()["revision"], "reason": "별도 확정",
    })
    assert confirmed.status_code == 200, confirmed.text
    link = confirmed.json()["evidence_links"][0]
    assert link["status"] == "confirmed"
    assert link["reason"] == "후보 메모"
    assert link["confirm_reason"] == "별도 확정"
    assert confirmed.json()["evidence_ids"] == [evidence_id]
    assert {key: confirmed.json()[key] for key in money} == money
    assert client.get(prefix + "/journals").json() == []
    blocked = client.post(prefix + f"/transactions/{created['id']}/evidence-links/{evidence_id}/unlink", json={
        "expected_revision": confirmed.json()["revision"],
    })
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "EVIDENCE_LINK_NOT_CANDIDATE"
