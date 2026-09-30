"""Manifest of deployable source files; no runtime data or private material.

Only the application's package and named dependency inputs are inspected.
This module never reads a credential, session store, evidence-signing key or DB.
"""
from __future__ import annotations
import hashlib
from pathlib import Path


def runtime_source_manifest(package_root=None) -> dict:
    root = Path(package_root) if package_root is not None else Path(__file__).resolve().parent
    if root.is_symlink() or not root.is_dir():
        raise ValueError('RELEASE_SOURCE_ROOT')
    paths = []
    allowed = {'.py', '.sql', '.js', '.mjs', '.html', '.css', '.json'}
    for entry in root.rglob('*'):
        relative = entry.relative_to(root)
        if '__pycache__' in relative.parts:
            continue
        if entry.is_symlink():
            raise ValueError('RELEASE_SOURCE_SYMLINK')
        if entry.is_file() and entry.suffix in allowed:
            paths.append(entry)
    parent = root.parent
    for name in ('pyproject.toml', 'worker.json', 'Dockerfile'):
        target = parent / name
        if target.exists():
            paths.append(target)
    for pattern in ('requirements*.txt', 'requirements*.lock', 'uv.lock'):
        paths.extend(parent.glob(pattern))
    if len(paths) > 10_000:
        raise ValueError('RELEASE_SOURCE_LIMIT')
    records, total = [], 0
    for path in sorted(set(paths)):
        if path.is_symlink() or not path.is_file():
            raise ValueError('RELEASE_SOURCE_SYMLINK')
        before = path.stat()
        total += before.st_size
        if before.st_size > 10_000_000 or total > 100_000_000:
            raise ValueError('RELEASE_SOURCE_LIMIT')
        content = path.read_bytes()
        after = path.stat()
        if (before.st_ino, before.st_mtime_ns, before.st_size) != (after.st_ino, after.st_mtime_ns, after.st_size):
            raise ValueError('RELEASE_SOURCE_CHANGED_DURING_HASH')
        records.append({'path': path.relative_to(parent).as_posix(), 'bytes': len(content),
                        'sha256': hashlib.sha256(content).hexdigest()})
    return {'schema': 'price.runtime-source-manifest/1', 'files': records}


def unique_json_fields(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('RELEASE_DUPLICATE_FIELD')
        result[name] = value
    return result


def reject_json_constant(value):
    raise ValueError('RELEASE_NONFINITE_JSON')
