from __future__ import annotations

import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from price.api import create_app
from price.auth import passwordless_login_permitted
from price.cli import reject_login_disabled_exposure
from price.db import DomainError
from conftest import PASSWORD, World


def counts(w: World) -> tuple[int, int, int]:
    with w.db.connect() as c:
        users = c.execute("SELECT count(*) FROM users").fetchone()[0]
        companies = c.execute("SELECT count(*) FROM companies").fetchone()[0]
        owners = c.execute("SELECT count(*) FROM memberships WHERE role='owner'").fetchone()[0]
    return users, companies, owners


def sole_owner(w: World) -> None:
    with w.db.transaction() as c:
        _ = c.execute("DELETE FROM memberships WHERE user_id=? AND role='owner'", (w.other_owner.id,))


def loopback(w: World) -> TestClient:
    return TestClient(create_app(w.db.path), base_url="http://127.0.0.1")


def test_flag_off_keeps_password_rejection(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRICE_LOGIN_DISABLED", raising=False)
    before = counts(w)
    with loopback(w) as client:
        denied = client.post("/api/login", json={"username": w.owner.username, "password": "wrong-password"})
        assert denied.status_code == 401
        assert denied.json()["code"] == "INVALID_LOGIN"
        assert "price_session" not in denied.cookies
        assert client.get("/api/session").status_code == 401
    assert counts(w) == before


def test_loopback_sole_owner_session_ignores_request_identity(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRICE_LOGIN_DISABLED", "1")
    monkeypatch.delenv("PRICE_ALLOW_PUBLIC_BIND", raising=False)
    sole_owner(w)
    before = counts(w)
    with loopback(w) as client:
        issued = client.post(
            "/api/login",
            json={"username": w.viewer.username, "password": "not-a-password", "role": "owner"},
            headers={"x-username": w.viewer.username, "x-role": "owner"},
        )
        assert issued.status_code == 200
        body = issued.json()
        assert body["username"] == w.owner.username
        assert body["companies"][0]["role"] == "owner"
        assert "password" not in issued.text
        assert client.get("/api/session").json()["username"] == w.owner.username
        rejected = client.post("/api/logout", json={})
        assert rejected.status_code == 403
        assert rejected.json()["code"] == "CSRF_REJECTED"
    with w.db.connect() as c:
        viewer = c.execute(
            "SELECT role FROM memberships WHERE user_id=? AND company_id=?",
            (w.viewer.id, w.company),
        ).fetchone()
    assert viewer is not None and viewer[0] == "viewer"
    assert counts(w) == before


def test_two_owners_fail_without_creating_users(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRICE_LOGIN_DISABLED", "1")
    before = counts(w)
    assert before[2] >= 2
    with loopback(w) as client:
        denied = client.post("/api/login", json={"username": w.owner.username, "password": PASSWORD})
        assert denied.status_code == 401
        assert denied.json()["code"] == "SOLE_OWNER_REQUIRED"
        assert "price_session" not in denied.cookies
    assert counts(w) == before


def test_zero_owners_fail_without_creating_users(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRICE_LOGIN_DISABLED", "1")
    with w.db.transaction() as c:
        _ = c.execute("DELETE FROM memberships WHERE role='owner'")
    before = counts(w)
    assert before[2] == 0
    with loopback(w) as client:
        denied = client.post("/api/login", json={})
        assert denied.status_code == 401
        assert denied.json()["code"] == "SOLE_OWNER_REQUIRED"
    assert counts(w) == before


def test_non_loopback_and_spoofed_host_do_not_issue_session(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRICE_LOGIN_DISABLED", "1")
    sole_owner(w)
    with TestClient(create_app(w.db.path)) as client:
        denied = client.post("/api/login", json={}, headers={"host": "127.0.0.1"})
        assert denied.status_code == 422
        assert "price_session" not in denied.cookies
    assert passwordless_login_permitted("127.0.0.1", "0.0.0.0") is False
    assert passwordless_login_permitted("localhost", "localhost") is True
    assert passwordless_login_permitted("::1", "[::1]") is True
    assert passwordless_login_permitted("testserver", "127.0.0.1") is False


def test_public_bind_combo_rejected(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRICE_LOGIN_DISABLED", "1")
    monkeypatch.setenv("PRICE_ALLOW_PUBLIC_BIND", "1")
    monkeypatch.setenv("PRICE_SECURE_COOKIE", "1")
    with pytest.raises(DomainError) as blocked:
        reject_login_disabled_exposure("127.0.0.1")
    assert blocked.value.code == "PUBLIC_BIND_BLOCKED"
    assert passwordless_login_permitted("127.0.0.1", "127.0.0.1") is False
    env = os.environ.copy()
    env["PRICE_LOGIN_DISABLED"] = "1"
    env["PRICE_ALLOW_PUBLIC_BIND"] = "1"
    env["PRICE_SECURE_COOKIE"] = "1"
    for host in ("127.0.0.1", "0.0.0.0"):
        result = subprocess.run(
            [sys.executable, "-m", "price", "--db", str(w.db.path), "serve", "--host", host],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert result.returncode == 1
        assert "PUBLIC_BIND_BLOCKED" in result.stdout
    monkeypatch.delenv("PRICE_ALLOW_PUBLIC_BIND", raising=False)
    reject_login_disabled_exposure("127.0.0.1")
    with pytest.raises(DomainError) as exposed:
        reject_login_disabled_exposure("0.0.0.0")
    assert exposed.value.code == "PUBLIC_BIND_BLOCKED"
