"""Separate bounded policy-bookkeeping executor. No model or financial network tools.

`--mode demo` can only read a company explicitly marked synthetic demo.
`--mode released` requires the existing code-bound release acceptance service.
The proposal-only worker and normal HTTP API remain unable to post this way.
"""
from __future__ import annotations
import argparse,json,signal,time
from .auth import Actor
from .automation import AutomaticBooks
from .db import Database,DomainError
from .service import Service
from .pipeline_health import WorkerHeartbeat
from .release_readiness import ReleaseReadiness


def eligible(service,company,mode):
    with service.db.connect() as c:
        row=c.execute('SELECT body FROM companies WHERE id=?',(company,)).fetchone()
        if not row:raise DomainError('NOT_FOUND','회사 작업공간을 찾을 수 없습니다.',404)
        profile=json.loads(row[0])
        grants=[g for g in service.db.list(c,company,'automation_grant') if g['state']=='ACTIVE']
        if mode=='demo':
            if profile.get('demo') is not True:raise DomainError('AUTOMATION_NOT_DEMO','실제 회사 공간을 데모 실행기로 처리할 수 없습니다.',403)
            return
        if mode!='released':raise DomainError('AUTOMATION_MODE','지원하지 않는 실행 모드입니다.',403)
        owner=c.execute("SELECT user_id FROM memberships WHERE company_id=? AND role='owner' ORDER BY user_id LIMIT 1",(company,)).fetchone()
        if not owner:raise DomainError('AUTOMATION_OWNER','현재 회사 소유자 권한이 필요합니다.',403)
    report=ReleaseReadiness(service).report(Actor(owner[0],'release-owner'),company)
    if not report.get('final_accepted'):raise DomainError('AUTOMATION_RELEASE_NOT_ACCEPTED','실제 회사의 무인 실행에는 현재 코드와 결합된 운영 인수가 필요합니다.',403)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True)
    parser.add_argument('--company',required=True)
    parser.add_argument('--mode',choices=('demo','released'),required=True)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    service=Service(Database(args.db))
    eligible(service,args.company,args.mode)
    engine=AutomaticBooks(service,execution_enabled=True)
    heartbeat=WorkerHeartbeat(service.db,worker_name='price-automatic-books')
    def stop(signum,frame):raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,stop)
    try:
        while True:
            # Eligibility is rechecked every cycle rather than cached at startup.
            eligible(service,args.company,args.mode)
            heartbeat.record(force=args.once)
            result=engine.run_once(args.company)
            if result['processed'] or args.once:
                print(json.dumps({k:result[k] for k in ('processed','state','journal_count','held_checks') if k in result}),flush=True)
            heartbeat.record(result)
            if args.once:break
            time.sleep(.5 if result['processed'] else 5)
    except KeyboardInterrupt:pass
    finally:heartbeat.stopped()


if __name__=='__main__':
    try:main()
    except DomainError as error:
        print(json.dumps({'processed':False,'code':error.code,'state':'NOT_EXECUTED'}),flush=True)
        raise SystemExit(2)
