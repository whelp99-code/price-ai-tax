# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi==0.141.1", "uvicorn==0.52.4", "pydantic==2.13.5", "httpx==0.28.1"]
# ///
# Run: <existing-release-python> scripts/release_health_smoke.py --release <prepared-SHA-directory>
"""One real release /health request with an empty readonly fixture, then teardown."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Generator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final
from unittest.mock import patch

from typing_extensions import override

ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.prepare_release import TARGET, ReleaseError, verify_release

if TYPE_CHECKING:
    from price.db import Database


class Arguments(argparse.Namespace):
    repo: Path = ROOT
    release: Path = Path()
    python: Path = Path(sys.executable)
    child: bool = False
    fixture: Path | None = None
    fixture_root: Path | None = None
    report: Path | None = None


def readonly_database_class(fixture_root: Path) -> type[Database]:
    """Adapt only the smoke factory; production Database code stays untouched."""
    from price.db import Database

    root = fixture_root.resolve()
    if not root.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise ReleaseError("FIXTURE_ROOT_NOT_TEMPORARY")

    @dataclass(frozen=True, slots=True)
    class ReadOnlyDatabase(Database):
        """An existing synthetic schema with URI-enforced readonly connections."""

        path: Path

        def __post_init__(self) -> None:
            if self.path.name == "company.sqlite3":
                raise ReleaseError("PRODUCTION_DATABASE_FORBIDDEN")
            resolved = self.path.resolve(strict=True)
            if self.path != resolved or not resolved.is_relative_to(root):
                raise ReleaseError("FIXTURE_OUTSIDE_TEMPORARY_ROOT")
            if stat.S_IMODE(self.path.stat().st_mode) != 0o400:
                raise ReleaseError("FIXTURE_NOT_READONLY")
            with self.connect() as connection:
                if connection.execute("SELECT 1 FROM schema_version WHERE version=1").fetchone() is None:
                    raise ReleaseError("FIXTURE_SCHEMA_INVALID")

        @override
        @contextmanager
        def connect(self) -> Generator[sqlite3.Connection, None, None]:
            if self.path.name == "company.sqlite3" or not self.path.is_relative_to(root):
                raise ReleaseError("CONNECTION_OUTSIDE_FIXTURE")
            if self.path != self.path.resolve(strict=True):
                raise ReleaseError("CONNECTION_PATH_CHANGED")
            connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            _ = connection.execute("PRAGMA query_only=ON")
            try:
                yield connection
            finally:
                connection.close()

    return ReadOnlyDatabase


def prove_readonly(database: Database, fixture_root: Path) -> None:
    """Prove SQLite URI protection persists even without the query_only flag."""
    if database.path.name == "company.sqlite3" or database.path.parent != fixture_root.resolve():
        raise ReleaseError("PROOF_OUTSIDE_FIXTURE")
    database.path.chmod(0o600)
    try:
        with database.connect() as connection:
            for query_only in ("ON", "OFF"):
                _ = connection.execute(f"PRAGMA query_only={query_only}")
                try:
                    _ = connection.execute("INSERT INTO schema_version VALUES(2)")
                except sqlite3.OperationalError as exc:
                    if exc.sqlite_errorcode != sqlite3.SQLITE_READONLY:
                        raise
                else:
                    raise ReleaseError("FIXTURE_WRITE_WAS_ALLOWED")
    finally:
        database.path.chmod(0o400)


def serve_readonly(repo: Path, release: Path, fixture: Path, fixture_root: Path) -> None:
    """Import the actual prepared API and run a single loopback smoke process."""
    _ = verify_release(repo, release)
    sys.path.insert(0, str(release))
    import uvicorn

    from price import api
    from price.db import Database

    if Path(api.__file__).resolve() != release / "price/api.py":
        raise ReleaseError("API_SOURCE_NOT_TARGET_RELEASE")
    print("SMOKE_STAGE API_SOURCE_OK", file=sys.stderr, flush=True)
    root = fixture_root.resolve()
    if fixture.name == "company.sqlite3" or fixture.parent != root or fixture.exists():
        raise ReleaseError("FIXTURE_PROVISIONING_BOUNDARY")
    if not root.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise ReleaseError("FIXTURE_ROOT_NOT_TEMPORARY")
    databases: list[Database] = []

    def capture_database(path: str | Path) -> Database:
        database = Database(path)
        databases.append(database)
        return database

    with patch.object(api, "Database", capture_database):
        app = api.create_app(fixture)
    with closing(sqlite3.connect(fixture)) as connection, connection:
        if connection.execute("SELECT 1 FROM companies LIMIT 1").fetchone() is not None:
            raise ReleaseError("FIXTURE_COMPANY_WAS_CREATED")
        if connection.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None:
            raise ReleaseError("FIXTURE_ACTOR_WAS_CREATED")
        _ = connection.execute("PRAGMA journal_mode=DELETE").fetchall()
    extra_files = {path for path in root.rglob("*") if path.is_file()} - {fixture}
    if extra_files:
        extensions = "_".join(sorted({re.sub("[^A-Z_]", "_", path.suffix.upper()) for path in extra_files}))
        raise ReleaseError("FIXTURE_UNEXPECTED_FILE_CREATED_" + extensions)
    fixture.chmod(0o400)
    database_type = readonly_database_class(fixture_root)
    _ = database_type(fixture)
    if len(databases) != 1 or databases[0].path != fixture:
        raise ReleaseError("API_DATABASE_NOT_FIXTURE")
    with patch.object(Database, "connect", database_type.connect):
        prove_readonly(databases[0], fixture_root)
        print("SMOKE_STAGE READONLY_SCHEMA_OK", file=sys.stderr, flush=True)
        print("READONLY_BOUNDARY_OK", file=sys.stderr, flush=True)
        uvicorn.run(app, host="127.0.0.1", port=0, access_log=False, log_level="info")


def wait_ready(process: subprocess.Popen[bytes]) -> int:
    """Subscribe to the bound-server log with one bounded startup deadline."""
    stderr = process.stderr
    if stderr is None:
        raise ReleaseError("SERVER_LOG_NOT_CONNECTED")
    deadline = time.monotonic() + 30
    readonly_proved = False
    stage = "INITIAL"
    buffered = bytearray()
    with selectors.DefaultSelector() as selector:
        _ = selector.register(stderr, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise ReleaseError("SERVER_STARTUP_TIMEOUT")
            chunk = os.read(stderr.fileno(), 4096)
            if not chunk:
                raise ReleaseError("SERVER_EXITED_BEFORE_READY_" + stage)
            buffered.extend(chunk)
            while b"\n" in buffered:
                line, _, tail = buffered.partition(b"\n")
                buffered.clear()
                buffered.extend(tail)
                observed_stage = re.search(rb"SMOKE_STAGE ([A-Z_]+)", line)
                if observed_stage:
                    stage = observed_stage.group(1).decode("ascii")
                failure = re.search(rb"CHILD_FAILURE ([A-Z_]+)", line)
                if failure:
                    raise ReleaseError("CHILD_" + failure.group(1).decode("ascii") + "_" + stage)
                exception = re.match(rb"(TypeError|ValueError|RuntimeError|AttributeError|ImportError|ModuleNotFoundError|NameError):", line)
                if exception:
                    raise ReleaseError("CHILD_" + exception.group(1).decode("ascii").upper() + "_" + stage)
                readonly_proved = readonly_proved or b"READONLY_BOUNDARY_OK" in line
                match = re.search(rb"Uvicorn running on http://127\.0\.0\.1:(\d+)", line)
                if match:
                    port = int(match.group(1))
                    if not readonly_proved or port == 4382:
                        raise ReleaseError("READONLY_SERVER_BOUNDARY_FAILED")
                    return port


def health_smoke(repo: Path, release: Path, python: Path) -> dict[str, str | int | bool]:
    """Use one HTTP request, always terminate the child and remove its fixture."""
    release = release.expanduser().resolve()
    _ = verify_release(repo, release)
    with tempfile.TemporaryDirectory(prefix="price-release-health-") as temporary:
        root = Path(temporary)
        fixture = root / "health.sqlite3"
        environment = {
            "PATH": os.defpath,
            "PYTHONPATH": str(release),
            "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": "C.UTF-8",
            "HOME": str(root),
        }
        command = [
            str(python), str(Path(__file__).resolve()), "--child",
            "--repo", str(repo), "--release", str(release),
            "--fixture", str(fixture), "--fixture-root", str(root),
        ]
        with subprocess.Popen(
            command, cwd=root, env=environment, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, bufsize=0,
        ) as process:
            try:
                port = wait_ready(process)
                before = hashlib.sha256(fixture.read_bytes()).hexdigest()
                url = f"http://127.0.0.1:{port}/health"
                print(f"HTTP_REQUEST {url}", file=sys.stderr, flush=True)
                response = subprocess.run(
                    [
                        "curl", "--include", "--silent", "--show-error", "--fail-with-body",
                        "--max-time", "10", "--write-out", "\nHEALTH_STATUS:%{http_code}\n", url,
                    ],
                    capture_output=True, text=True, check=True, timeout=15,
                )
                raw, status = response.stdout.rsplit("\nHEALTH_STATUS:", 1)
                if status.strip() != "200":
                    raise ReleaseError("HEALTH_NOT_200")
            finally:
                process.terminate()
                try:
                    _ = process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    _ = process.wait(timeout=10)
        unchanged = before == hashlib.sha256(fixture.read_bytes()).hexdigest()
        if not unchanged or stat.S_IMODE(fixture.stat().st_mode) != 0o400:
            raise ReleaseError("FIXTURE_CHANGED")
        if fixture.with_name(fixture.name + "-wal").exists() or fixture.with_name(fixture.name + "-shm").exists():
            raise ReleaseError("FIXTURE_WAL_CREATED")
        result: dict[str, str | int | bool] = {
            "status": 200, "url": url, "http_response": raw,
            "release": str(release), "commit": TARGET,
            "api_source": str(release / "price/api.py"),
            "readonly_uri_proved": True, "fixture_unchanged": unchanged,
            "server_terminated": True, "server_pid": process.pid,
        }
    if root.exists():
        raise ReleaseError("FIXTURE_CLEANUP_FAILED")
    result["fixture_removed"] = True
    return result


def main() -> None:
    """Run either the controlled child or the one-request smoke supervisor."""
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--repo", type=Path, default=ROOT)
    _ = parser.add_argument("--release", type=Path, required=True)
    _ = parser.add_argument("--python", type=Path, default=Path(sys.executable))
    _ = parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    _ = parser.add_argument("--fixture", type=Path, help=argparse.SUPPRESS)
    _ = parser.add_argument("--fixture-root", type=Path, help=argparse.SUPPRESS)
    _ = parser.add_argument("--report", type=Path)
    args = parser.parse_args(namespace=Arguments())
    try:
        if args.child:
            if args.fixture is None or args.fixture_root is None:
                raise ReleaseError("FIXTURE_ARGUMENTS_REQUIRED")
            serve_readonly(args.repo, args.release.resolve(), args.fixture, args.fixture_root)
            return
        result = health_smoke(args.repo, args.release, args.python)
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.report is not None:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            _ = args.report.write_text(text + "\n")
        print(text)
    except ReleaseError as exc:
        if args.child:
            print(f"CHILD_FAILURE {exc}", file=sys.stderr, flush=True)
        print(json.dumps({"status": "REJECTED", "reason": str(exc)}))
        raise SystemExit(1) from exc
    except (OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        if args.child:
            reason = f"IO_{type(exc).__name__.upper()}"
            if isinstance(exc, sqlite3.Error):
                reason += "_" + exc.sqlite_errorname
            frames = traceback.extract_tb(exc.__traceback__)
            if frames:
                reason += "_" + re.sub("[^A-Z_]", "_", (Path(frames[-1].filename).stem + "_" + frames[-1].name).upper())
            print(f"CHILD_FAILURE {reason}", file=sys.stderr, flush=True)
        print(json.dumps({"status": "REJECTED", "reason": "SMOKE_IO_FAILED"}))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
