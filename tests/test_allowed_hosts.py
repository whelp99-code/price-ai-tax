import os

from fastapi.testclient import TestClient

from price.api import create_app


def test_production_allowlist_rejects_testserver(monkeypatch, tmp_path):
    monkeypatch.delenv("PRICE_ALLOW_TESTSERVER", raising=False)
    monkeypatch.delenv("PRICE_ALLOWED_HOSTS", raising=False)
    app = create_app(tmp_path / "hosts.sqlite3")
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 400
    assert response.json()["code"] == "HOST_NOT_ALLOWED"


def test_readcheck_host_is_allowed_only_from_env(monkeypatch, tmp_path):
    monkeypatch.delenv("PRICE_ALLOW_TESTSERVER", raising=False)
    monkeypatch.setenv("PRICE_ALLOWED_HOSTS", "price-readcheck.internal")
    app = create_app(tmp_path / "hosts.sqlite3")
    with TestClient(app, base_url="http://price-readcheck.internal") as client:
        allowed = client.get("/health")
    with TestClient(app) as client:
        blocked = client.get("/health")
    assert allowed.status_code == 200
    assert blocked.status_code == 400
    assert blocked.json()["code"] == "HOST_NOT_ALLOWED"
    assert os.getenv("PRICE_ALLOWED_HOSTS") == "price-readcheck.internal"


def test_testserver_is_allowed_only_when_the_test_flag_is_set(monkeypatch, tmp_path):
    monkeypatch.setenv("PRICE_ALLOW_TESTSERVER", "1")
    monkeypatch.delenv("PRICE_ALLOWED_HOSTS", raising=False)
    app = create_app(tmp_path / "hosts.sqlite3")
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
