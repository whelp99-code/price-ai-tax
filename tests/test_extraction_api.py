from __future__ import annotations
import sqlite3
import pytest
from test_owner_upload_api import web, start, send


def uploaded(web,w):
    app,client,path=web;batch=start(client,path)
    assert send(client,path,batch).status_code==200
    sealed=client.post(path+'/uploads/'+batch['id']+'/complete',json={'expected_revision':batch['revision']}).json()
    doc=sealed['files'][0]['document']['id']
    assert sealed['source_reading'][0]['state'] == 'SUCCEEDED'
    assert app.state.source_reader.run_once(w.company)['processed'] is False
    return app,client,path,doc


def test_authenticated_reprocess_coverage_history_http(web,w):
    app,client,path,doc=uploaded(web,w)
    prefix=path+'/documents/'+doc
    view=client.get(prefix).json()
    coverage=client.get(prefix+'/coverage').json()
    assert coverage['record_count']==2 and not coverage['company_period_complete']
    payload={'expected_revision':view['revision'],'idempotency_key':'api-reprocess-001','reason':'원본 재확인'}
    queued=client.post(prefix+'/reprocess',json=payload)
    assert queued.status_code==200,queued.text
    assert queued.json()['generation']==2
    assert client.get(prefix+'/extraction').json()['total']==0
    assert app.state.source_reader.run_once(w.company)['state']=='SUCCEEDED'
    assert client.get(prefix+'/extraction').json()['total']==2
    history=client.get(prefix+'/extraction/history')
    assert history.status_code==200 and len(history.json()['runs'])==2
    assert client.post(prefix+'/reprocess',json=payload).json()['reused']


def test_reprocess_preserves_csrf_and_client_server_field_boundary(web,w):
    app,client,path,doc=uploaded(web,w)
    prefix=path+'/documents/'+doc
    version=client.get(prefix).json()['revision']
    payload={'expected_revision':version,'idempotency_key':'api-reprocess-001','reason':'원본 재확인'}
    assert client.post(prefix+'/reprocess',json=payload,headers={'x-csrf-token':'incorrect'}).status_code==403
    assert client.post(prefix+'/reprocess',json={**payload,'approved_by':'model','parser_url':'https://example.invalid'}).status_code==422
    assert client.get(prefix).json()['revision']==version


def test_snapshot_tables_refuse_mutation(web,w):
    app,client,path,doc=uploaded(web,w)
    with w.db.connect() as c:
        row=c.execute("SELECT id FROM objects WHERE company_id=? AND kind='extraction_run'",(w.company,)).fetchone()
        assert row
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE objects SET body='{}' WHERE company_id=? AND kind='extraction_run' AND id=?",(w.company,row['id']))
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("DELETE FROM objects WHERE company_id=? AND kind='extraction_run' AND id=?",(w.company,row['id']))


def test_owner_home_shows_current_source_count_not_retired_cases(web,w):
    app,client,path,doc=uploaded(web,w)
    prefix=path+'/documents/'+doc;version=client.get(prefix).json()['revision']
    client.post(prefix+'/reprocess',json={'expected_revision':version,'idempotency_key':'api-reprocess-002','reason':'원본 재확인'})
    app.state.source_reader.run_once(w.company)
    assert client.get(path+'/owner/home').json()['counts']['source_cases']==2
