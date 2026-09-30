#!/usr/bin/env python3
"""Create-only installation into an independent price directory. No services or Git writes."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import sys
ROOT=Path(__file__).resolve().parents[1]
EXCLUDES={'.data','.venv','.git','.chatgpt2codex','__pycache__','.pytest_cache','dist','build','artifacts','.env'}

def install(destination:Path,source:Path=ROOT)->dict:
    destination=destination.expanduser().absolute();source=source.resolve()
    if destination.exists() or destination.is_symlink():raise ValueError('TARGET_EXISTS: existing target will not be modified')
    resolved=destination.resolve()
    if resolved.is_relative_to(source):raise ValueError('RECURSIVE_DESTINATION: target cannot be inside source package')
    if any(p.lower()=='corp-os' for p in resolved.parts):raise ValueError('NOT_STANDALONE: do not install into CORP-OS')
    if resolved.name!='price':raise ValueError('PROJECT_NAME: destination basename must be price')
    if not (source/'pyproject.toml').is_file() or not (source/'price'/'api.py').is_file():raise ValueError('SOURCE_INVALID: run from an extracted price package')
    if any(p.is_symlink() for p in source.rglob('*') if not set(p.relative_to(source).parts)&EXCLUDES):raise ValueError('SOURCE_SYMLINK: source package must not contain symlinks')
    destination.parent.mkdir(parents=True,exist_ok=True)
    shutil.copytree(source,destination,ignore=lambda p,names:[n for n in names if n in EXCLUDES or n.startswith('.env.') or n.endswith(('.pyc','.sqlite3','.sqlite3-wal','.sqlite3-shm','.db','.egg-info','.whl'))])
    return {'project':'price','worker':'price','destination':str(destination),'source_copy':'complete','runtime_initialized':False,'service_started':False,'worker_registered':False,'existing_projects_modified':False,'next_document':'README.md'}

def main():
    p=argparse.ArgumentParser();p.add_argument('--destination',required=True,type=Path);a=p.parse_args()
    try:print(json.dumps(install(a.destination),ensure_ascii=False,indent=2))
    except (ValueError,OSError) as exc:
        print(json.dumps({'status':'NOT_INSTALLED','error':str(exc),'note':'Existing destinations are never overwritten. I/O errors may leave a partial new directory.'}));sys.exit(1)
if __name__=='__main__':main()
