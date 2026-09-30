#!/usr/bin/env python3
"""Real Ubuntu loopback E2E, synthetic records only; stops only its own process."""
from __future__ import annotations
import base64
import json
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
import httpx
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from price.db import Database
from price.service import Service
from price.models import CompanyInput,UserInput
PASSWORD='Synthetic-HTTP-Test-2026!'

def main():
    evidence=ROOT/'evidence'/'ubuntu';evidence.mkdir(parents=True,exist_ok=True)
    (ROOT/'.data').mkdir(exist_ok=True)
    report={'started_at':datetime.now(timezone.utc).isoformat(),'runtime':platform.platform(),'project_root':str(ROOT),'data':'synthetic only','real_tax_bank_requests':0,'checks':[]}
    def check(name,condition=True):
        assert condition,name
        report['checks'].append({'name':name,'status':'PASS'})
    with tempfile.TemporaryDirectory(prefix='http-',dir=ROOT/'.data') as tmp:
        dbpath=Path(tmp)/'price.sqlite3';db=Database(dbpath);svc=Service(db)
        company,owner=svc.auth.bootstrap(CompanyInput(name='HTTP 종단 시험 법인'),'http-owner',PASSWORD)
        svc.add_user(owner,company,UserInput(username='http-reviewer',password=PASSWORD,role='reviewer'))
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        url=f'http://127.0.0.1:{port}';report['server_url_at_test_time']=url
        with (evidence/'http-e2e-server.log').open('w') as log:
            process=subprocess.Popen([sys.executable,'-m','price','--db',str(dbpath),'serve','--port',str(port)],cwd=ROOT,stdout=log,stderr=log)
            try:
                with httpx.Client(base_url=url,timeout=10,trust_env=False) as client:
                    for _ in range(120):
                        if process.poll() is not None:raise RuntimeError('server exited: '+str(process.returncode))
                        try:
                            if client.get('/health').status_code==200:break
                        except httpx.HTTPError:pass
                        time.sleep(.1)
                    else:raise RuntimeError('health timeout')
                    report['health']=client.get('/health').json();check('HTTP health / independent product',report['health']['product']=='AI 세무사')
                    prefix=f'/api/companies/{company}'
                    def login(user):
                        r=client.post('/api/login',json={'username':user,'password':PASSWORD});assert r.status_code==200,r.text
                        client.headers['x-csrf-token']=r.json()['csrf']
                    def post(path,body,expected=200):
                        r=client.post(prefix+path,json=body);assert r.status_code==expected,(path,r.status_code,r.text)
                        return r.json()
                    def get(path):
                        r=client.get(prefix+path);assert r.status_code==200,r.text;return r.json()
                    check('unauthenticated records refused',client.get(prefix+'/transactions').status_code==401)
                    login('http-owner');check('owner login')
                    ev=post('/evidence',{'filename':'synthetic.txt','content_base64':base64.b64encode(b'SYNTHETIC - NOT REAL TAX EVIDENCE').decode()},201)
                    check('original upload hash download',len(ev['sha256'])==64 and client.get(prefix+'/evidence/'+ev['id']+'/download').status_code==200)
                    def invoice(key,direction,net,tax):return dict(external_id=key,direction=direction,event_date='2026-09-15',counterparty='시험 거래처',net=net,tax=tax,gross=net+tax,tax_category='standard',deductible=True,business_purpose='합성 시험',evidence_ids=[ev['id']])
                    sale=post('/transactions',invoice('sale-1','sale',1000,100),201)
                    purchase=post('/transactions',invoice('purchase-1','purchase',300,30),201)
                    repeated=post('/transactions',invoice('sale-1','sale',1000,100),201)
                    check('repeat import returns same ID',repeated['id']==sale['id'] and repeated['deduplicated'])
                    journals=[post('/transactions/'+t['id']+'/analyze',{})['journal'] for t in (sale,purchase)]
                    error=post('/journals/'+journals[0]['id']+'/approve',{'expected_revision':journals[0]['revision']},403)
                    check('self approval rejected on HTTP',error['code']=='SELF_APPROVAL')
                    login('http-reviewer')
                    approved=[post('/journals/'+j['id']+'/approve',{'expected_revision':j['revision'],'reason':'독립 시험 승인'}) for j in journals]
                    login('http-owner')
                    for j in approved:assert post('/journals/'+j['id']+'/post',{'expected_revision':j['revision']})['state']=='POSTED'
                    check('independent approval then posting')
                    bank=post('/transactions',dict(external_id='bank-1',kind='bank',direction='receipt',event_date='2026-09-16',counterparty='시험 거래처',net=1100,tax=0,gross=1100,business_purpose='합성 입금',evidence_ids=[ev['id']]),201)
                    check('unreconciled bank asks facts',post('/transactions/'+bank['id']+'/analyze',{})['case']['state']=='NEEDS_FACTS')
                    post('/allocations',dict(bank_id=bank['id'],invoice_id=sale['id'],amount=1100,reason='HTTP 모의 대사'),201)
                    j=post('/transactions/'+bank['id']+'/analyze',{})['journal'];login('http-reviewer')
                    j=post('/journals/'+j['id']+'/approve',{'expected_revision':j['revision'],'reason':'대사 검토'});login('http-owner')
                    post('/journals/'+j['id']+'/post',{'expected_revision':j['revision']});check('bank invoice allocation and posting')
                    p=post('/periods',dict(label='2026-09 합성',start='2026-09-01',end='2026-09-30'),201)
                    check('period blocked before coverage',not get('/periods/'+p['id']+'/readiness')['ready'])
                    p=post('/periods/'+p['id']+'/coverage',dict(expected_revision=p['revision'],opening_reviewed=True,sources_confirmed=['manual'],evidence_note='합성 기초자료·범위 수기 확인'))
                    p=post('/periods/'+p['id']+'/close',dict(expected_revision=p['revision'],reason='합성 월마감'))
                    check('period closes after conditions',p['closed'])
                    calc=post('/calculations',{'period_id':p['id'],'tax_type':'VAT'},201)
                    check('VAT review not official tax',calc['totals']['net_vat']==70 and not calc['official_form'] and not calc['blockers'])
                    f=post('/filings',{'calculation_id':calc['id'],'label':'모의 신고'},201);login('http-reviewer')
                    f=post('/filings/'+f['id']+'/approve',dict(expected_revision=f['revision'],reason='모의 계산·마감 검토'))
                    check('live filing refuses',post('/filings/'+f['id']+'/submit',dict(expected_revision=f['revision'],environment='production'),409)['code']=='LIVE_CHANNEL_UNCONFIGURED')
                    r=post('/filings/'+f['id']+'/submit',dict(expected_revision=f['revision'],scenario='timeout_after_accept'))
                    check('timeout remains unknown',r['filing']['state']=='SUBMISSION_UNKNOWN')
                    check('unknown cannot resubmit',post('/filings/'+f['id']+'/submit',dict(expected_revision=f['revision']),409)['code']=='RECONCILE_REQUIRED')
                    received=post('/filings/'+f['id']+'/reconcile',{})['filing']
                    check('receipt explicitly simulated',received['state']=='SIMULATED_RECEIVED' and not received['receipt']['is_real_filing'])
                    payment=post('/filings/'+f['id']+'/payments',dict(amount=70,evidence_id=ev['id'],note='모의 수기 기록'),201)
                    check('payment bank unverified',not payment['bank_verified'] and get('/filings/'+f['id'])['payment_status']=='UNCONFIRMED')
                    consult=post('/consultations',dict(question='현재 미해결 사항을 알려주세요',as_of='2026-09-28'),201)
                    check('model configuration honest',consult['provider_status']=='NOT_CONFIGURED')
                    post('/transactions/'+sale['id']+'/enqueue',{});r=post('/worker/run',{})
                    check('durable worker no duplicate posting',r['state']=='SUCCEEDED' and len([j for j in get('/journals') if j['state']=='POSTED'])==3)
                    exported=get('/export')
                    check('auditable export not official',exported['audit_integrity']['valid'] and not exported['official_form'])
                    check('debit credit exact match',get('/trial-balance')['balanced'])
                    report['final_counts']=get('/overview')['counts'];report['audit']=get('/audit')['integrity'];report['status']='PASS'
            except Exception as exc:
                report['status']='FAIL';report['error']=str(exc);raise
            finally:
                process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:process.kill();process.wait()
                report['server_stopped']=True;report['finished_at']=datetime.now(timezone.utc).isoformat()
                (evidence/'http-e2e-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({'status':report['status'],'checks':len(report['checks']),'file':'evidence/ubuntu/http-e2e-report.json'},ensure_ascii=False))
if __name__=='__main__':main()
