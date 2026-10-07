from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from price import cli
from price.api import create_app
from price.auth import Actor
from price.db import DomainError
from price.sql_types import read_row, row_value
from tests.conftest import PASSWORD, World


def http_client(candidate: object) -> httpx.Client:
    # Starlette's installed compatibility path uses the declared httpx dependency.
    if not isinstance(candidate, httpx.Client):
        raise TypeError("Expected the native httpx client")
    return candidate


def test_disable_revokes_live_sessions_blocks_login_and_preserves_other_users(w: World) -> None:
    token_one, _ = w.svc.auth.login(w.viewer.username, PASSWORD)
    token_two, _ = w.svc.auth.login(w.viewer.username, PASSWORD)
    other_token, _ = w.svc.auth.login(w.preparer.username, PASSWORD)
    with TestClient(create_app(w.db.path)) as native_client:
        client = http_client(native_client)
        for token in (token_one, token_two):
            client.cookies.set("price_session", token)
            assert client.get("/api/session").status_code == 200
        result = w.svc.auth.disable_user(w.owner, w.company, w.viewer.id)
        assert result == {"id": w.viewer.id, "disabled": True, "sessions_revoked": 2}
        for token in (token_one, token_two):
            client.cookies.set("price_session", token)
            assert client.get("/api/session").status_code == 401
        response = client.post("/api/login", json={"username": w.viewer.username, "password": PASSWORD})
        assert response.status_code == 429
        assert response.json()["code"] == "LOGIN_LOCKED"
        client.cookies.set("price_session", other_token)
        assert client.get("/api/session").status_code == 200
        assert client.post("/api/login", json={"username": w.preparer.username, "password": PASSWORD}).status_code == 200
    with w.db.connect() as c:
        assert c.execute("SELECT count(*) FROM sessions WHERE user_id=?", (w.viewer.id,)).fetchone()[0] == 0
        audit = read_row(c.execute(
            "SELECT actor,detail FROM audit WHERE company_id=? AND action='member.disabled' AND entity_id=?",
            (w.company, w.viewer.id),
        ))
        assert audit is not None
        assert audit["actor"] == w.owner.id
        detail = row_value(audit, "detail", str)
        assert json.loads(detail) == {"sessions_revoked": 2}
        assert w.db.verify_audit(c, w.company)["valid"]


def test_cli_disable_user_uses_native_owner_and_clears_its_admin_session(
    w: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    token, _ = w.svc.auth.login(w.viewer.username, PASSWORD)
    monkeypatch.setattr("sys.argv", [
        "price", "--db", str(w.db.path), "disable-user",
        "--company", w.company, "--user", w.viewer.id, "--admin", w.owner.username,
    ])
    def password_input(_prompt: str) -> str:
        return PASSWORD

    monkeypatch.setattr("getpass.getpass", password_input)
    cli.main()
    assert json.loads(capsys.readouterr().out) == {
        "id": w.viewer.id, "disabled": True, "sessions_revoked": 1,
    }
    with pytest.raises(DomainError) as revoked:
        _ = w.svc.auth.session(token)
    assert revoked.value.status == 401
    with w.db.connect() as c:
        assert c.execute("SELECT count(*) FROM sessions WHERE user_id=?", (w.owner.id,)).fetchone()[0] == 0


@pytest.mark.parametrize("actor_name,status", [("viewer", 403), ("preparer", 403), ("reviewer", 403), ("other_owner", 404)])
def test_disable_requires_native_company_owner_without_mutation(w: World, actor_name: str, status: int) -> None:
    token, _ = w.svc.auth.login(w.viewer.username, PASSWORD)
    with pytest.raises(DomainError) as denied:
        actors = {"viewer": w.viewer, "preparer": w.preparer, "reviewer": w.reviewer, "other_owner": w.other_owner}
        _ = w.svc.auth.disable_user(actors[actor_name], w.company, w.viewer.id)
    assert denied.value.status == status
    assert w.svc.auth.session(token).id == w.viewer.id
    with w.db.connect() as c:
        assert c.execute("SELECT disabled FROM users WHERE id=?", (w.viewer.id,)).fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM audit WHERE action='member.disabled'").fetchone()[0] == 0


def test_disable_worker_foreign_user_and_shared_account_are_denied(w: World) -> None:
    worker = Actor(w.owner.id, w.owner.username, worker=True)
    for actor, target in ((worker, w.viewer.id), (w.owner, w.other_owner.id)):
        with pytest.raises(DomainError) as denied:
            _ = w.svc.auth.disable_user(actor, w.company, target)
        assert denied.value.status == 403
    with w.db.transaction() as c:
        _ = c.execute("INSERT INTO memberships(company_id,user_id,role) VALUES(?,?,'viewer')", (w.other_company, w.viewer.id))
    token, _ = w.svc.auth.login(w.viewer.username, PASSWORD)
    with pytest.raises(DomainError) as denied:
        _ = w.svc.auth.disable_user(w.owner, w.company, w.viewer.id)
    assert denied.value.status == 404
    assert w.svc.auth.session(token).id == w.viewer.id
    with w.db.connect() as c:
        assert c.execute("SELECT disabled FROM users WHERE id=?", (w.viewer.id,)).fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM audit WHERE action='member.disabled'").fetchone()[0] == 0


def test_viewer_reads_company_reports_but_cannot_write_or_read_foreign_company(w: World) -> None:
    with w.db.transaction() as c:
        _ = c.execute(
            "INSERT INTO objects(company_id,kind,id,revision,body,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (w.company, "expert_review", "active-review-source-test", 1,
             json.dumps({"state": "UNASSIGNED", "event_id": "missing-source-event",
                         "source_context_hash": "missing-source", "fact_context_hash": "missing-facts"}),
             "2026-10-07T00:00:00Z", "2026-10-07T00:00:00Z"),
        )
    with TestClient(create_app(w.db.path)) as native_client:
        client = http_client(native_client)
        login = client.post("/api/login", json={"username": w.viewer.username, "password": PASSWORD})
        assert login.status_code == 200
        headers = {"x-csrf-token": login.json()["csrf"]}
        for suffix in ("owner/reports?start=2026-09-01&end=2026-09-30", "discrepancies", "event-settlements"):
            assert client.get(f"/api/companies/{w.company}/{suffix}").status_code == 200
            assert client.get(f"/api/companies/{w.other_company}/{suffix}").status_code == 404
        inbox = client.get(f"/api/companies/{w.company}/discrepancies")
        assert inbox.json()["items"][0]["id"] == "active-review-source-test"
        assert inbox.json()["items"][0]["freshness"] == "STALE"
        body = {"username": "revocation-test-new-user", "password": PASSWORD, "role": "viewer"}
        assert client.post(f"/api/companies/{w.company}/users", json=body, headers=headers).status_code == 403
