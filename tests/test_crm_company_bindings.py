from copy import deepcopy
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from price import crm_bindings
from price.api import create_app
from price.auth import Actor
from price.crm_bindings import CRMCompanyBindings, CRMCompanyCandidate
from price.db import DomainError


@pytest.fixture
def binding(w, monkeypatch):
    service, owner, company, other = w.svc, w.owner, w.company, w.other_company
    engine = CRMCompanyBindings(service)
    data = CRMCompanyCandidate(
        crm_workspace_id=UUID("11111111-2222-4333-8444-555555555555"),
        price_company_id=company,
        source_reference="https://github.com/whelp99-code/ai-crm-project-manager/issues/47#issuecomment-6064552247",
    )
    monkeypatch.setattr(crm_bindings, "OWNER_REGISTRATION", data.model_dump(mode="json"))
    return service, owner, company, other, engine, data


def test_candidate_is_one_native_row_with_audited_actor_and_pending_human(binding):
    service, owner, company, _, engine, data = binding
    row = engine.register(owner, company, data)
    assert row["registered_by"] == "omonyang-root"
    assert row["registered_actor_id"] == owner.id
    assert row["registered_at"]
    assert row["confirmed_by_human"] is False
    assert row["price_company_id"] == company
    assert row["crm_workspace_id"] == str(data.crm_workspace_id)
    assert engine.register(owner, company, data) == row
    assert engine.get(owner, company)["confirmed_connection"] is False
    with service.db.connect() as c:
        assert len(service.db.list(c, company, "crm_company_binding")) == 1
        assert c.execute("SELECT count(*) FROM audit WHERE action='crm_company_binding.registered'").fetchone()[0] == 1
        assert service.db.verify_audit(c, company)["valid"] is True


def test_conflicting_or_confirmed_binding_is_not_overwritten(binding):
    service, owner, company, _, engine, data = binding
    original = engine.register(owner, company, data)
    conflict = data.model_copy(update={"crm_workspace_id": UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")})
    with pytest.raises(DomainError) as failure:
        engine.register(owner, company, conflict)
    assert failure.value.status == 409
    with service.db.connect() as c:
        human = deepcopy(original)
        human["confirmed_by_human"] = True
        service.db.replace(c, company, "crm_company_binding", original["id"], human, original["revision"])
    with pytest.raises(DomainError):
        engine.register(owner, company, data)
    assert engine.get(owner, company)["candidate"]["confirmed_by_human"] is True


def test_worker_viewer_and_other_company_are_denied(binding):
    service, owner, company, other, engine, data = binding
    with service.db.connect() as c:
        viewer_row = c.execute("SELECT u.id,u.username FROM users u JOIN memberships m ON m.user_id=u.id WHERE m.company_id=? AND m.role='viewer'", (company,)).fetchone()
    viewer = Actor(viewer_row["id"], viewer_row["username"])
    for actor in (viewer, Actor("worker", "worker", worker=True)):
        with pytest.raises(DomainError) as failure:
            engine.register(actor, company, data)
        assert failure.value.status == 403
    with pytest.raises(DomainError) as failure:
        engine.get(viewer, other)
    assert failure.value.status == 404
    with pytest.raises(DomainError):
        engine.register(owner, other, data)
    with service.db.connect() as c:
        assert service.db.list(c, company, "crm_company_binding") == []
        assert service.db.list(c, other, "crm_company_binding") == []


def test_http_requires_native_owner_and_confirmation_reference(binding, w):
    service, _, company, _, _, data = binding
    with TestClient(create_app(service.db.path)) as client:
        response = client.post("/api/login", json={"username": w.owner.username, "password": "Test-Only-Price-2026!"})
        headers = {"x-csrf-token": response.json()["csrf"]}
        payload = data.model_dump(mode="json")
        path = f"/api/companies/{company}/crm-company-binding"
        assert client.post(path, json={**payload, "confirmed_by_human": True}, headers=headers).status_code == 422
        created = client.post(path, json=payload, headers=headers)
        assert created.status_code == 201 and created.json()["confirmed_by_human"] is False
        assert client.get(path).json()["confirmed_connection"] is False


def test_recorded_owner_confirmation_is_identity_only_and_idempotent(binding):
    service, owner, company, _, engine, data = binding
    confirmed = CRMCompanyCandidate.model_validate({**data.model_dump(mode="json"),
        "confirmed_by_human": True, "confirmed_by": "owner",
        "confirmation_ref": "C0C6HJM0BV5:1791500798.801419",
    })
    row = engine.register(owner, company, confirmed)
    assert row["confirmed_by_human"] is True
    assert row["confirmed_by"] == "owner"
    assert row["confirmation_ref"] == confirmed.confirmation_ref
    assert engine.register(owner, company, confirmed) == row
    assert engine.get(owner, company)["confirmed_connection"] is True
    with service.db.connect() as c:
        assert service.db.list(c, company, "crm_company_binding") == [row]
        assert service.db.list(c, company, "journal") == []
        assert service.db.verify_audit(c, company)["valid"] is True


def test_owner_decision_cannot_confirm_another_workspace(binding):
    _, _, _, _, _, data = binding
    payload = {**data.model_dump(mode="json"), "confirmed_by_human": True,
               "confirmed_by": "owner", "confirmation_ref": "C0C6HJM0BV5:1791500798.801419",
               "crm_workspace_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"}
    with pytest.raises(ValueError):
        CRMCompanyCandidate.model_validate(payload)


def test_packaged_registration_retains_the_explicit_owner_confirmation():
    registration = CRMCompanyCandidate.model_validate(crm_bindings.OWNER_REGISTRATION)
    assert registration.confirmed_by_human is True
    assert registration.confirmed_by == "owner"
    assert registration.confirmation_ref == "C0C6HJM0BV5:1791500798.801419"
