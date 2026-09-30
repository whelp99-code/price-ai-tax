"""Release acceptance must bind security dependencies, not only feature filenames."""
from pathlib import Path
import json
import pytest
from price.release_readiness import runtime_source_manifest,verified_envelope
from price.db import digest
from test_release_readiness import envelope,synthetic_test_evidence


def tree(tmp_path):
    package=tmp_path/'price';package.mkdir()
    for name in ('auth.py','db.py','api.py','source_controls.py','schema.sql'):
        (package/name).write_text('# SYNTHETIC SOURCE '+name)
    (package/'static').mkdir();(package/'static'/'owner.js').write_text('/* SYNTHETIC UI */')
    (tmp_path/'requirements.txt').write_text('example==1.0\n')
    (tmp_path/'requirements-readers.lock').write_text('# SYNTHETIC READER LOCK\n')
    (tmp_path/'pyproject.toml').write_text('[project]\nname="synthetic"\n')
    return package


def test_all_runtime_dependencies_are_bound_not_only_curated_features(tmp_path):
    package=tree(tmp_path);before=runtime_source_manifest(package)
    files={f['path'] for f in before['files']}
    assert {'price/auth.py','price/db.py','price/source_controls.py','price/schema.sql','price/static/owner.js','requirements.txt','requirements-readers.lock','pyproject.toml'}<=files
    (package/'auth.py').write_text('# Changed source authorization')
    assert digest(runtime_source_manifest(package))!=digest(before)


def test_new_module_and_removed_module_invalidate_acceptance(tmp_path):
    package=tree(tmp_path);first=digest(runtime_source_manifest(package))
    (package/'new_rule.py').write_text('# New rule')
    assert digest(runtime_source_manifest(package))!=first
    (package/'new_rule.py').unlink()
    assert digest(runtime_source_manifest(package))==first
    (package/'db.py').unlink()
    assert digest(runtime_source_manifest(package))!=first


def test_runtime_data_and_caches_are_not_hashed_or_disclosed(tmp_path):
    package=tree(tmp_path);first=runtime_source_manifest(package)
    (tmp_path/'.data').mkdir();(tmp_path/'.data'/'private.sqlite3').write_bytes(b'PRIVATE TEST DATA')
    (package/'__pycache__').mkdir();(package/'__pycache__'/'auth.cpython-314.pyc').write_bytes(b'CACHE')
    assert runtime_source_manifest(package)==first
    assert 'PRIVATE' not in json.dumps(first)


def test_symlink_runtime_source_is_not_followed(tmp_path):
    package=tree(tmp_path);outside=tmp_path/'outside.py';outside.write_text('outside')
    (package/'escape.py').symlink_to(outside)
    with pytest.raises(ValueError):runtime_source_manifest(package)


def test_duplicate_envelope_fields_cannot_be_shadowed():
    raw,key=envelope(synthetic_test_evidence())
    duplicate=raw.replace(b'"schema": ',b'"schema": "untrusted", "schema": ',1)
    with pytest.raises(ValueError):verified_envelope(duplicate,key,'b'*64)


def test_nonfinite_json_is_not_allowed_in_signed_evidence():
    raw,key=envelope([])
    # An unverified malformed body must yield a controlled error, not acceptance.
    invalid=raw.replace(b'"records": []',b'"records": [{"duration":NaN}]')
    with pytest.raises(ValueError):verified_envelope(invalid,key,'b'*64)
