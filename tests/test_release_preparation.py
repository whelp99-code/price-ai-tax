"""Pinned source preparation and real readonly health smoke; synthetic DBs only."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from price.api import create_app
from scripts import release_health_smoke as readonly_health
from scripts.prepare_release import (
    MANIFEST,
    ROOT,
    TARGET,
    ReleaseError,
    prepare_release,
    verify_release,
)


@pytest.fixture
def prepared(tmp_path: Path) -> Path:
    report = prepare_release(ROOT, tmp_path / "releases")
    return Path(report["release"])


@pytest.fixture
def readonly_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "health.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        _ = connection.executescript((ROOT / "price/schema.sql").read_text())
    path.chmod(0o400)
    return path


def test_prepared_source_matches_git_object_and_existing_target_is_untouched(prepared: Path) -> None:
    expected = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{TARGET}:price/api.py"],
        capture_output=True, check=True, timeout=10,
    ).stdout
    assert (prepared / "price/api.py").read_bytes() == expected
    before = (prepared / MANIFEST).read_bytes()
    with pytest.raises(ReleaseError, match="TARGET_EXISTS"):
        _ = prepare_release(ROOT, prepared.parent)
    assert (prepared / MANIFEST).read_bytes() == before
    assert (prepared / "price/api.py").read_bytes() == expected
    assert verify_release(ROOT, prepared)["activated"] is False


@pytest.mark.parametrize("commit", ["main", "a" * 40, "7469d467d41c5f78a29d8093e0588e34e89896e4"])
def test_unreviewed_commit_is_rejected_before_creating_directory(tmp_path: Path, commit: str) -> None:
    target = tmp_path / "releases"
    with pytest.raises(ReleaseError, match="COMMIT_NOT_PINNED"):
        _ = prepare_release(ROOT, target, commit)
    assert not target.exists()


def test_existing_broken_symlink_is_not_replaced(tmp_path: Path) -> None:
    root = tmp_path / "releases"
    root.mkdir()
    destination = root / TARGET
    destination.symlink_to(tmp_path / "absent")
    with pytest.raises(ReleaseError, match="TARGET_EXISTS"):
        _ = prepare_release(ROOT, root)
    assert destination.is_symlink()


def test_tampered_release_is_rejected_before_smoke_start(prepared: Path) -> None:
    _ = (prepared / "price/api.py").write_text("# changed synthetic release copy\n")
    with pytest.raises(ReleaseError, match="RELEASE_SOURCE_MISMATCH"):
        _ = readonly_health.health_smoke(ROOT, prepared, Path(sys.executable))


def test_uri_remains_readonly_after_permissions_and_query_flag_change(readonly_fixture: Path) -> None:
    database_type = readonly_health.readonly_database_class(readonly_fixture.parent)
    database = database_type(readonly_fixture)
    before = readonly_fixture.read_bytes()
    readonly_fixture.chmod(0o600)
    try:
        with database.connect() as connection:
            _ = connection.execute("PRAGMA query_only=OFF")
            with pytest.raises(sqlite3.OperationalError) as error:
                _ = connection.execute("INSERT INTO schema_version VALUES(2)")
        assert error.value.sqlite_errorcode == sqlite3.SQLITE_READONLY
        assert readonly_fixture.read_bytes() == before
    finally:
        readonly_fixture.chmod(0o400)


def test_production_path_and_outside_fixture_are_rejected(readonly_fixture: Path) -> None:
    database_type = readonly_health.readonly_database_class(readonly_fixture.parent)
    with pytest.raises(ReleaseError, match="PRODUCTION_DATABASE_FORBIDDEN"):
        _ = database_type(Path.home() / ".local/share/price/company.sqlite3")
    outside_type = readonly_health.readonly_database_class(readonly_fixture.parent / "owned")
    with pytest.raises(ReleaseError, match="FIXTURE_OUTSIDE_TEMPORARY_ROOT"):
        _ = outside_type(readonly_fixture)
    readonly_fixture.chmod(0o600)
    with pytest.raises(ReleaseError, match="FIXTURE_NOT_READONLY"):
        _ = database_type(readonly_fixture)


def test_unadapted_api_constructor_is_not_a_readonly_start(readonly_fixture: Path) -> None:
    with pytest.raises(sqlite3.OperationalError):
        _ = create_app(readonly_fixture)


def test_cli_empty_schema_never_requires_owner_creation_or_listens(readonly_fixture: Path) -> None:
    readonly_fixture.chmod(0o600)
    result = subprocess.run(
        [
            sys.executable, "-m", "price", "--db", str(readonly_fixture),
            "serve", "--host", "127.0.0.1", "--port", "0",
        ],
        capture_output=True, text=True, check=False, timeout=10,
    )
    body = TypeAdapter(dict[str, str]).validate_json(result.stdout)
    assert result.returncode == 1
    assert body["error"] == "NOT_INITIALIZED"


def test_actual_release_health_and_cleanup(prepared: Path) -> None:
    result = readonly_health.health_smoke(ROOT, prepared, Path(sys.executable))
    assert result["status"] == 200
    assert result["readonly_uri_proved"] is True
    assert result["fixture_unchanged"] is True
    assert result["server_terminated"] is True
    assert result["fixture_removed"] is True
    response = result["http_response"]
    assert isinstance(response, str)
    headers, body = response.split("\n\n", 1)
    health = TypeAdapter(dict[str, str | bool]).validate_json(body)
    assert headers.startswith("HTTP/1.1 200")
    assert health["ok"] is True and health["project"] == "price"
    assert health["live_filing"] is False
