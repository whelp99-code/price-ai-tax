from datetime import datetime,timezone,timedelta
import json,subprocess,sys
import pytest
from test_owner_upload_api import web
from test_autopolicies import activated
from test_automation import ready
from price.automation import AutomaticBooks,AutomationAction
from price.db import DomainError


def test_owner_can_consent_without_tax_fields_http_does_not_post(web,w):
    app,client,path=web
    _,policy,event,_=activated(w)
    data={'policy_ids':[policy['id']],'period_start':'2026-09-01','period_end':'2026-09-30',
          'expires_at':(datetime.now(timezone.utc)+timedelta(days=2)).isoformat(),'max_per_event':1100,
          'max_total':1100,'max_events':1,'idempotency_key':'http-consent-01','confirmed':True}
    response=client.post(path+'/automation/consents',json=data)
    assert response.status_code==201,response.text
    view=client.get(path+'/owner/automation').json()
    assert view['execution_enabled'] is False and view['posted_events']==0
    assert client.post(path+'/automation/consents',json={**data,'approved_by':w.owner.id,'posting_enabled':True}).status_code==422
    assert client.post(path+'/automation/consents',json=data,headers={'x-csrf-token':'incorrect'}).status_code==403
    grant=response.json()
    stopped=client.post(path+'/automation/consents/'+grant['id']+'/stop',json={'expected_revision':grant['revision'],'reason':'범위 재확인'})
    assert stopped.status_code==200 and stopped.json()['state']=='STOPPED'
    with w.db.connect() as c:assert not w.db.list(c,w.company,'journal')


def test_real_policy_child_worker_processes_demo_only(w):
    engine,grant,event,_,_=ready(w)
    command=[sys.executable,'-m','price.automatic_worker','--db',str(w.db.path),'--company',w.company,'--mode','demo','--once']
    refused=subprocess.run(command,capture_output=True,text=True,timeout=15)
    assert refused.returncode==2 and json.loads(refused.stdout)['code']=='AUTOMATION_NOT_DEMO'
    # Mark only this test fixture's company as a synthetic test tenant.
    with w.db.transaction() as c:
        profile=json.loads(c.execute('SELECT body FROM companies WHERE id=?',(w.company,)).fetchone()[0])
        profile['demo']=True;c.execute('UPDATE companies SET body=? WHERE id=?',(json.dumps(profile),w.company))
    first=subprocess.run(command,capture_output=True,text=True,timeout=15)
    assert first.returncode==0,first.stderr
    assert json.loads(first.stdout.strip().splitlines()[-1])['journal_count']==1
    second=subprocess.run(command,capture_output=True,text=True,timeout=15)
    assert second.returncode==0 and not json.loads(second.stdout.strip().splitlines()[-1])['processed']
    with w.db.connect() as c:
        assert len(w.db.list(c,w.company,'journal'))==1
        assert len(w.db.list(c,w.company,'automation_effect'))==1
        assert w.db.verify_audit(c,w.company)['valid']


def test_real_company_worker_refuses_unaccepted_release(w):
    engine,grant,event,_,_=ready(w)
    p=subprocess.run([sys.executable,'-m','price.automatic_worker','--db',str(w.db.path),'--company',w.company,'--mode','released','--once'],capture_output=True,text=True,timeout=15)
    assert p.returncode==2,p.stderr
    assert json.loads(p.stdout)['code']=='AUTOMATION_RELEASE_NOT_ACCEPTED'
    with w.db.connect() as c:assert not w.db.list(c,w.company,'journal')


def test_usage_counter_cannot_be_reset_to_renew_limit(w):
    engine,grant,event,_,_=ready(w)
    engine.process(w.company,grant['id'],event['id'])
    with w.db.transaction() as c:
        current=w.db.get(c,w.company,'automation_grant',grant['id'])
        w.db.replace(c,w.company,'automation_grant',grant['id'],{**current,'processed_events':0,'processed_gross':0},current['revision'])
    with pytest.raises(DomainError) as error:engine.process(w.company,grant['id'],event['id'])
    assert error.value.code=='AUTOMATION_USAGE_MISMATCH'


def test_effect_and_ledger_are_both_immutable(w):
    import sqlite3
    engine,grant,event,_,_=ready(w);result=engine.process(w.company,grant['id'],event['id'])
    with w.db.connect() as c:
        with pytest.raises(sqlite3.IntegrityError):c.execute("DELETE FROM objects WHERE company_id=? AND kind='automation_effect'",(w.company,))
        with pytest.raises(sqlite3.IntegrityError):c.execute("UPDATE objects SET body='{}' WHERE company_id=? AND kind='automation_effect'",(w.company,))
