# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# Run: python scripts/prepare_release.py --repo . --releases-root ~/.local/share/price/releases
"""Create or verify the pinned source release. Never activate a service or open a DB."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from typing import Final, TypedDict

TARGET: Final = "4b2623a22488d7038e5c132ce87a398a3f5045c8"
MANIFEST: Final = ".price-release.json"
ROOT: Final = Path(__file__).resolve().parents[1]


class ReleaseError(ValueError):
    """A preparation boundary rejected the requested operation."""


class ReleaseReport(TypedDict):
    release: str
    commit: str
    source_files: int
    activated: bool


class Arguments(argparse.Namespace):
    repo: Path = ROOT
    releases_root: Path = Path.home() / ".local/share/price/releases"
    commit: str = TARGET
    verify_only: bool = False


def source_archive(repo: Path, commit: str) -> bytes:
    """Export only the exact reviewed Git object, never the working tree."""
    if commit != TARGET:
        raise ReleaseError("COMMIT_NOT_PINNED")
    result = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", commit],
        check=True, capture_output=True, timeout=60,
    )
    return result.stdout


def source_hashes(raw: bytes) -> dict[str, str]:
    """Validate archive boundaries and hash its regular source files."""
    files: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                raise ReleaseError("UNSAFE_ARCHIVE")
            if any(part in {".git", ".data", ".venv", "__pycache__"} or part.startswith(".env") for part in path.parts):
                raise ReleaseError("PRIVATE_RUNTIME_FILE")
            if path.suffix in {".sqlite3", ".db", ".pyc"}:
                raise ReleaseError("PRIVATE_RUNTIME_FILE")
            if member.isfile():
                content = archive.extractfile(member)
                if content is None:
                    raise ReleaseError("ARCHIVE_CONTENT_MISSING")
                with content:
                    files[member.name] = hashlib.sha256(content.read()).hexdigest()
    if not {"price/api.py", "price/db.py", "price/schema.sql", "pyproject.toml"} <= files.keys():
        raise ReleaseError("SOURCE_NOT_PRICE")
    return files


def manifest_text(commit: str, files: dict[str, str]) -> str:
    """Serialize only source binding metadata, never runtime or company records."""
    return json.dumps({"commit": commit, "files": files}, sort_keys=True, indent=2) + "\n"


def verify_release(repo: Path, release: Path, commit: str = TARGET) -> ReleaseReport:
    """Compare a prepared tree with its pinned archive and exact manifest."""
    if release.is_symlink() or not release.is_dir():
        raise ReleaseError("RELEASE_NOT_DIRECTORY")
    files = source_hashes(source_archive(repo, commit))
    actual: dict[str, str] = {}
    for path in release.rglob("*"):
        if path.is_symlink():
            raise ReleaseError("RELEASE_SYMLINK")
        if path.is_file() and path.relative_to(release).as_posix() != MANIFEST:
            actual[path.relative_to(release).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != files or (release / MANIFEST).read_text() != manifest_text(commit, files):
        raise ReleaseError("RELEASE_SOURCE_MISMATCH")
    return {"release": str(release), "commit": commit, "source_files": len(files), "activated": False}


def prepare_release(repo: Path, releases_root: Path, commit: str = TARGET) -> ReleaseReport:
    """Create only a new SHA directory; existing releases are never overwritten."""
    repo = repo.resolve()
    releases_root = releases_root.expanduser().resolve()
    if releases_root.is_relative_to(repo):
        raise ReleaseError("RELEASE_INSIDE_SOURCE")
    raw = source_archive(repo, commit)
    files = source_hashes(raw)
    destination = releases_root / commit
    if destination.exists() or destination.is_symlink():
        raise ReleaseError("TARGET_EXISTS")
    releases_root.mkdir(parents=True, exist_ok=True)
    destination.mkdir(mode=0o700)
    try:
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            archive.extractall(destination, filter="data")
        _ = (destination / MANIFEST).write_text(manifest_text(commit, files))
        return verify_release(repo, destination, commit)
    except (OSError, tarfile.TarError, ReleaseError, subprocess.SubprocessError):
        shutil.rmtree(destination)
        raise


def main() -> None:
    """Prepare or verify source only, with safe machine-readable output."""
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--repo", type=Path, default=ROOT)
    _ = parser.add_argument("--releases-root", type=Path, default=Arguments.releases_root)
    _ = parser.add_argument("--commit", default=TARGET)
    _ = parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(namespace=Arguments())
    try:
        if args.verify_only:
            report = verify_release(args.repo, args.releases_root.expanduser() / args.commit, args.commit)
        else:
            report = prepare_release(args.repo, args.releases_root, args.commit)
    except ReleaseError as exc:
        print(json.dumps({"status": "REJECTED", "reason": str(exc)}))
        raise SystemExit(1) from exc
    except (OSError, tarfile.TarError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "REJECTED", "reason": "PREPARATION_IO_FAILED"}))
        raise SystemExit(1) from exc
    print(json.dumps(report))


if __name__ == "__main__":
    main()
