"""Create and smoke-test a data-free development source archive after verification."""
from __future__ import annotations
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime,timezone
from pathlib import Path
from verify_engineering_snapshot import manifest

ROOT=Path(__file__).resolve().parents[1]


def main():
    reports=sorted((ROOT/'evidence/version-loop/snapshots').glob('*/verification.json'))
    if not reports:raise RuntimeError('No version-bound verification snapshot exists')
    proof_path=reports[-1];proof=json.loads(proof_path.read_text())
    if proof.get('status')!='VERIFIED_ENGINEERING_SNAPSHOT' or proof.get('source_files')!=manifest():
        raise RuntimeError('Sources changed or verification failed; rerun verification before packaging')
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder=ROOT/'artifacts';folder.mkdir(exist_ok=True)
    target=folder/('price-development-'+stamp+'.zip')
    sources=[]
    for directory in ('price','tests','scripts'):
        for path in (ROOT/directory).rglob('*'):
            if path.is_file() and not path.is_symlink() and '__pycache__' not in path.parts and path.suffix in ('.py','.sql','.js','.mjs','.css','.html'):
                sources.append(path)
    names=['AGENTS.md','README.md','.gitignore','pyproject.toml','requirements.txt','requirements-test.txt','requirements-readers.lock','worker.json',
           'docs/DEVELOPMENT_PLAN_KO.md','docs/OWNER_UPLOAD_FIRST_REQUIREMENTS_KO.md','docs/releases/ENGINEERING_COMPONENTS_20260928.md','docs/releases/versions.json',
           'evidence/version-loop/business-facts/report.json','evidence/version-loop/V01/owner-extended-browser-report.json','evidence/ubuntu/browser-report.json','evidence/ubuntu/http-e2e-report.json']
    sources.extend(ROOT/name for name in names if (ROOT/name).is_file() and not (ROOT/name).is_symlink())
    sources.extend([proof_path,proof_path.with_name('regression.xml')])
    for name in ('docs/releases/RECONNECTED_CASHFLOW_20260929.md','evidence/version-loop/cashflow-browser/report.json'):
        path=ROOT/name
        if path.is_file() and not path.is_symlink():sources.append(path)
    file_hashes={}
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(set(sources)):
            if not path.resolve().is_relative_to(ROOT) or '.data' in path.relative_to(ROOT).parts:raise RuntimeError('Non-source file selected')
            content=path.read_bytes()
            if re.search(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|sk-[A-Za-z0-9_-]{30,}',content):
                raise RuntimeError('Potential private material detected; archive must be reviewed')
            relative=path.relative_to(ROOT).as_posix();archive.writestr(relative,content)
            file_hashes[relative]=hashlib.sha256(content).hexdigest()
        archive.writestr('SOURCE_MANIFEST.json',json.dumps({'kind':'development-not-final-release','final_accepted':False,'files':file_hashes,'verification':proof_path.relative_to(ROOT).as_posix()},ensure_ascii=False,indent=2))
    if proof['source_files']!=manifest():raise RuntimeError('Source changed during packaging; archive is not approved')
    with zipfile.ZipFile(target) as archive:
        listed=json.loads(archive.read('SOURCE_MANIFEST.json'))
        for name,expected in listed['files'].items():
            if hashlib.sha256(archive.read(name)).hexdigest()!=expected:raise RuntimeError('Archive member hash mismatch')
        verification_root=Path(tempfile.mkdtemp(prefix='package-smoke-',dir=ROOT/'.data'))
        archive.extractall(verification_root)
    code='''import json,sys
from pathlib import Path
import price
from price.db import Database
from price.service import Service
from price.demo import seed_demo
from price.api import create_app
from fastapi.testclient import TestClient
assert Path(price.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())
db=Database(Path("package-smoke.sqlite3"));seed_demo(Service(db))
app=create_app(db.path)
with TestClient(app) as client:
    assert client.get("/health").json()["live_filing"] is False
    assert "owner.js" in client.get("/").text
    assert "app.js" in client.get("/expert").text
    login=client.post("/api/login",json={"username":"demo-owner","password":"DemoOnly-Price-2026!"})
    assert login.status_code==200
    prefix="/api/companies/"+login.json()["companies"][0]["id"]
    assert client.get(prefix+"/owner/events").status_code==200
    assert client.get(prefix+"/ops/release-readiness").json()["final_accepted"] is False
    assert client.get(prefix+"/ops/pipeline").status_code==200
print(json.dumps({"source_import":True,"schema":True,"static_assets":True,"authenticated_api":True,"final_release_claimed":False}))
'''
    run=subprocess.run([sys.executable,'-c',code,str(verification_root)],cwd=verification_root,capture_output=True,text=True,timeout=30)
    if run.returncode!=0:
        print(run.stderr[-3000:]);raise RuntimeError('Isolated source archive smoke test failed')
    report={'schema':'price.development-package/1','source_archive':target.relative_to(ROOT).as_posix(),'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
            'bytes':target.stat().st_size,'source_files':len(file_hashes),'member_hashes_verified':True,'isolated_source_smoke':json.loads(run.stdout.strip().splitlines()[-1]),
            'verification':proof_path.relative_to(ROOT).as_posix(),'regression':proof['regression'],'source_stable':True,'final_version_accepted':False,
            'runtime_db_credentials_and_originals_included':False,'pip_install_performed':False}
    destination=target.with_suffix('.json');destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
