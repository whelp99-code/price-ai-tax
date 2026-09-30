"""Create a project-local, non-destructive source and SQLite checkpoint."""
from __future__ import annotations
import hashlib
import json
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target = ROOT / '.data' / 'release-checkpoints' / stamp
    target.mkdir(parents=True, mode=0o700)
    files = []
    for folder in ('price', 'tests', 'scripts', 'docs'):
        for path in (ROOT / folder).rglob('*'):
            if path.is_file() and not path.is_symlink() and '__pycache__' not in path.parts and path.suffix in ('.py', '.sql', '.js', '.mjs', '.css', '.html', '.md', '.json'):
                files.append(path)
    for name in ('AGENTS.md', 'README.md', 'pyproject.toml', 'requirements.txt', 'requirements-test.txt', '.gitignore', 'worker.json'):
        path = ROOT / name
        if path.is_file() and not path.is_symlink():
            files.append(path)
    manifest = {'created_at': stamp, 'project': 'price', 'files': {}, 'databases': []}
    with zipfile.ZipFile(target / 'source.zip', 'x', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(set(files)):
            raw = path.read_bytes()
            rel = str(path.relative_to(ROOT))
            archive.writestr(rel, raw)
            manifest['files'][rel] = hashlib.sha256(raw).hexdigest()
    for name in ('demo.sqlite3', 'price.sqlite3'):
        source = ROOT / '.data' / name
        if not source.is_file() or source.is_symlink():
            continue
        output = target / name
        with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src, sqlite3.connect(output) as dst:
            src.backup(dst)
            integrity = dst.execute('PRAGMA integrity_check').fetchone()[0]
        output.chmod(0o600)
        if integrity != 'ok':
            raise RuntimeError('Checkpoint integrity check failed')
        manifest['databases'].append({'name': name, 'integrity': integrity})
    (target / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'checkpoint': str(target.relative_to(ROOT)), 'source_files': len(files), 'database_count': len(manifest['databases']), 'integrity': 'ok'}))


if __name__ == '__main__':
    main()
