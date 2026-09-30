#!/usr/bin/env python3
"""Project-local preview supervisor. No system service, public bind, or other-process control."""
from __future__ import annotations
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import httpx

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'.data'
STATE=DATA/'runtime.json'
PORT=4381


def identity(pid):
    try:
        proc=Path('/proc')/str(pid)
        fields=(proc/'stat').read_text().rsplit(')',1)[1].split()
        return {'cwd':str((proc/'cwd').resolve()),'args':(proc/'cmdline').read_bytes().decode().split('\0')[:-1],'started':fields[19]}
    except (OSError,ValueError,IndexError):return None


def owned(record):
    current=identity(record.get('pid',0))
    return bool(current and current['cwd']==str(ROOT) and current==record.get('identity'))


def health():
    try:
        with httpx.Client(timeout=2,trust_env=False) as c:
            r=c.get(f'http://127.0.0.1:{PORT}/health')
            r.raise_for_status();return r.json()
    except (httpx.HTTPError,ValueError):return None


def read_state():
    if not STATE.exists():return {}
    state=json.loads(STATE.read_text())
    if state.get('schema')!='price.local-runtime/1' or state.get('root')!=str(ROOT):
        raise RuntimeError('Unknown runtime state: not modified')
    return state


def status():
    state=read_state();api=owned(state.get('api',{}));worker=owned(state.get('worker',{}))
    h=health() if api else None
    return {'project':'price','worker':'price','root':str(ROOT),'api_running':api,'api_health':h,'worker_running':worker,
            'url':f'http://127.0.0.1:{PORT}','bind':'127.0.0.1','database':str(DATA/'demo.sqlite3'),
            'api_pid':state.get('api',{}).get('pid'),'worker_pid':state.get('worker',{}).get('pid'),
            'bookkeeping_worker_enabled':bool(state.get('bookkeeping_enabled')),
            'bookkeeping_worker_running':owned(state.get('bookkeeping',{})),
            'bookkeeping_worker_pid':state.get('bookkeeping',{}).get('pid'),
            'bookkeeping_scope':'independently_approved_finite_schedules_only',
            'system_service_installed':False,'orca_worker_registered':False,'demo_only':True}


def save(state):
    tmp=STATE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(state,ensure_ascii=False,indent=2));os.chmod(tmp,0o600);tmp.replace(STATE)


def _start(bookkeeping=False):
    if type(bookkeeping) is not bool:raise ValueError('bookkeeping flag must be a boolean')
    DATA.mkdir(exist_ok=True,mode=0o700)
    state=read_state()
    enable_books=bookkeeping or state.get('bookkeeping_enabled') is True
    names=('api','worker','bookkeeping') if enable_books else ('api','worker')
    alive = {k: owned(state.get(k,{})) for k in names}
    if all(alive.values()):
        return status()
    db=DATA/'demo.sqlite3'
    if not db.exists():raise RuntimeError('Initialize synthetic demo DB explicitly first; no auto seeding')
    if not alive['api']:
        with socket.socket() as s:
            try:s.bind(('127.0.0.1',PORT))
            except OSError:raise RuntimeError('Port occupied: no process was stopped or replaced')
    state={**state,'schema':'price.local-runtime/1','root':str(ROOT),'created_at':state.get('created_at',time.time())}
    state['bookkeeping_enabled']=enable_books
    commands={
        'api':[sys.executable,'-m','price','--db',str(db),'serve','--host','127.0.0.1','--port',str(PORT)],
        'worker':[sys.executable,'-m','price.worker','--db',str(db)]}
    if enable_books:commands['bookkeeping']=[sys.executable,'-m','price.book_schedule_worker','--db',str(db)]
    created=[]
    try:
        for name,command in commands.items():
            if alive[name]:
                continue
            logpath=DATA/(name+'.log')
            with logpath.open('a') as log:
                os.chmod(logpath,0o600)
                proc=subprocess.Popen(command,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
            created.append(proc)
            for _ in range(20):
                info=identity(proc.pid)
                if info and info['args']==command:break
                time.sleep(.02)
            if not info or info['args']!=command:raise RuntimeError(name+' process identity not established')
            state[name]={'pid':proc.pid,'identity':info,'log':str(logpath)};save(state)
            if name=='api':
                for _ in range(100):
                    if proc.poll() is not None:raise RuntimeError('API exited before ready; see project-local api.log')
                    h=health()
                    if h and h.get('project')=='price' and h.get('live_filing') is False:break
                    time.sleep(.1)
                else:raise RuntimeError('API readiness timeout')
            elif proc.poll() is not None:raise RuntimeError('Worker exited')
        return status()
    except BaseException:
        for proc in created:
            if proc.poll() is None:
                proc.terminate()
                try:proc.wait(timeout=5)
                except subprocess.TimeoutExpired:pass
        raise


def _stop():
    state=read_state();stopped=[]
    for name in ('bookkeeping','worker','api'):
        rec=state.get(name,{})
        if owned(rec):
            os.kill(rec['pid'],signal.SIGTERM);stopped.append(name)
    for _ in range(50):
        if not any(owned(state.get(k,{})) for k in stopped):break
        time.sleep(.1)
    return {**status(),'stop_requested':stopped,'other_processes_touched':False}


def _locked_action(action):
    DATA.mkdir(exist_ok=True,mode=0o700)
    with (DATA/'runtime.lock').open('a') as guard:
        os.chmod(DATA/'runtime.lock',0o600)
        fcntl.flock(guard,fcntl.LOCK_EX)
        try:return action()
        finally:fcntl.flock(guard,fcntl.LOCK_UN)


def start(bookkeeping=False):return _locked_action(lambda:_start(bookkeeping))
def stop():return _locked_action(_stop)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['start','status','stop']);parser.add_argument('--bookkeeping',action='store_true',help='Enable only the separately reviewed finite-schedule executor');args=parser.parse_args()
    try:
        result=start(bookkeeping=args.bookkeeping) if args.action=='start' else {'status':status,'stop':stop}[args.action]()
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except Exception as exc:
        print(json.dumps({'error':str(exc),'root':str(ROOT)},ensure_ascii=False));raise SystemExit(1)

if __name__=='__main__':main()
