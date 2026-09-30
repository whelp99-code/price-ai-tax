"""Run the current test suites and bind their result to a stable source snapshot.

Only local synthetic test servers/DBs are used. This does not perform external
filing, install dependencies, or mark any product version accepted.
"""
from __future__ import annotations
import hashlib
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def manifest():
    result={}
    for folder in ('price','tests','scripts'):
        for path in sorted((ROOT/folder).rglob('*')):
            if '__pycache__' in path.parts or path.is_symlink() or not path.is_file():continue
            if path.suffix not in ('.py','.sql','.js','.mjs','.css','.html','.toml','.lock'):continue
            result[path.relative_to(ROOT).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ('pyproject.toml','requirements.txt','requirements-test.txt','requirements-readers.lock','worker.json','AGENTS.md'):
        path=ROOT/name
        if path.is_file() and not path.is_symlink():result[name]=hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def main():
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output=ROOT/'evidence/version-loop/snapshots'/stamp;output.mkdir(parents=True)
    before=manifest();records=[]
    junit=output/'regression.xml'
    commands=[('unit-integration',[sys.executable,'-m','pytest','-q','--junitxml='+str(junit)]),
              ('http-regression',[sys.executable,'scripts/e2e_http.py']),
              ('owner-files-browser',['node','scripts/e2e_owner_extended.mjs']),
              ('owner-business-facts-browser',['node','scripts/e2e_business_facts.mjs']),
              ('expert-regression-browser',['node','scripts/e2e_browser.mjs'])]
    if (ROOT/'scripts/e2e_reader_replay.mjs').is_file():commands.append(('reader-replay-browser',['node','scripts/e2e_reader_replay.mjs']))
    if (ROOT/'scripts/e2e_cashflow.mjs').is_file():commands.append(('cashflow-browser',['node','scripts/e2e_cashflow.mjs']))
    if (ROOT/'scripts/e2e_bookkeeping.mjs').is_file():commands.append(('bookkeeping-browser',['node','scripts/e2e_bookkeeping.mjs']))
    for name,command in commands:
        with (output/(name+'.log')).open('w',encoding='utf-8') as log:
            try:
                result=subprocess.run(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,timeout=100,check=False)
                code=result.returncode
            except subprocess.TimeoutExpired:code=124
        records.append({'name':name,'exit_code':code,'log':(output/(name+'.log')).relative_to(ROOT).as_posix()})
        print(json.dumps({'suite':name,'exit_code':code}),flush=True)
        if code!=0:break
    after=manifest()
    source_changed=before!=after
    totals={'tests':0,'failures':0,'errors':0,'skipped':0}
    if junit.exists():
        for suite in ET.parse(junit).getroot().iter('testsuite'):
            for key in totals:totals[key]+=int(suite.get(key,0))
    report={'schema':'price.engineering-verification/1','created_at':datetime.now(timezone.utc).isoformat(),'root':str(ROOT),
            'source_files':before,'source_stable':not source_changed,'changed_during_verification':sorted(set(before)^set(after)|{key for key in before.keys()&after.keys() if before[key]!=after[key]}),
            'suites':records,'regression':totals,'data_kind':'synthetic','final_version_accepted':False,
            'all_suites_passed':len(records)==len(commands) and all(r['exit_code']==0 for r in records),
            'release_limitations':['institution original-format acceptance pending','statutory rule and form coverage incomplete','live model/channel/professional and operational acceptance pending']}
    report['status']='VERIFIED_ENGINEERING_SNAPSHOT' if report['all_suites_passed'] and not source_changed else 'RECHECK_REQUIRED'
    target=output/'verification.json';target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':report['status'],'source_stable':not source_changed,'regression':totals,'report':target.relative_to(ROOT).as_posix()},ensure_ascii=False))
    if report['status']!='VERIFIED_ENGINEERING_SNAPSHOT':raise SystemExit(1)


if __name__=='__main__':main()
