import base64
import csv
import io
import json
import sqlite3
import pytest
from pydantic import ValidationError
from price.db import DomainError
from price.models import *
from price.calculations import rate_tax, progressive_tax, straight_line_schedule, validate_lines

@pytest.mark.parametrize('value', [True,1.5,'100',-1,10**12+1])
def test_money_not_coerced(value):
    with pytest.raises(ValidationError):
        TransactionInput(external_id='x',direction='sale',event_date='2026-09-01',counterparty='A',net=value,gross=100)

@pytest.mark.parametrize('fields', [dict(net=100,tax=10,gross=120),dict(kind='bank',direction='sale'),dict(kind='bank',direction='receipt'),dict(tax_category='exempt'),dict(state='POSTED'),dict(approved_by='fake')])
def test_consistency_and_server_owned_fields(fields):
    data=dict(external_id='x',direction='sale',event_date='2026-09-01',counterparty='A',net=100,tax=10,gross=110)
    with pytest.raises(ValidationError): TransactionInput(**(data|fields))

@pytest.mark.parametrize('rate', ['NaN','Infinity','-0.1','1.1','abc'])
def test_invalid_math_rates(rate):
    with pytest.raises(DomainError): rate_tax(100,rate)

def test_rounding_trace_and_progressive_math():
    assert rate_tax(15,'0.1')['tax']==1
    assert rate_tax(15,'0.1','half_up')['tax']==2
    r=progressive_tax(150,[{'upper':100,'rate':'0.1'},{'upper':None,'rate':'0.2'}])
    assert r['tax']==20 and 'simulation' in r['scope']

def test_unreached_bracket_still_validated():
    with pytest.raises(DomainError): progressive_tax(1,[{'upper':100,'rate':'0.1'},{'upper':None,'rate':'NaN'}])

def test_depreciation_conserves_integer_cost():
    values=straight_line_schedule(1001,1,12)
    assert len(values)==12 and sum(values)==1000 and values[-1]>=values[0]
    with pytest.raises(DomainError): straight_line_schedule(10,11,2)

def test_unbalanced_and_negative_lines_rejected():
    with pytest.raises(DomainError,match='일치'):
        validate_lines([{'account':'cash','side':'debit','amount':1},{'account':'equity','side':'credit','amount':2}])
    with pytest.raises(DomainError): validate_lines([{'account':'cash','side':'debit','amount':-1},{'account':'equity','side':'credit','amount':-1}])

def test_unknown_account_cannot_pass_balance_validation():
    with pytest.raises(DomainError) as error:
        validate_lines([{'account':'invented','side':'debit','amount':1},{'account':'cash','side':'credit','amount':1}])
    assert error.value.code=='UNKNOWN_ACCOUNT'

def test_idempotent_source_and_conflict(w):
    tx=w.tx(external_id='same')
    replay=w.tx(external_id='same')
    assert replay['deduplicated'] and tx['id']==replay['id']
    with pytest.raises(DomainError) as e: w.tx(external_id='same',net=2000,tax=200,gross=2200)
    assert e.value.code=='EXTERNAL_ID_CONFLICT'
    distinct=w.tx(external_id='different')
    assert distinct['id']!=tx['id']

def test_persistence_on_new_service(w):
    from price.service import Service
    from price.db import Database
    tx=w.tx()
    assert Service(Database(w.db.path)).get_object(w.owner,w.company,'transaction',tx['id'])['gross']==1100

@pytest.mark.parametrize('filename,raw', [('../x.txt',b'x'),('bad.exe',b'x'),('fake.pdf',b'not pdf'),('fake.png',b'x'),('bad.txt',b'\xff'),('nul.txt',b'\x00')])
def test_evidence_validation(w,filename,raw):
    with pytest.raises(DomainError): w.svc.upload_evidence(w.owner,w.company,filename,base64.b64encode(raw).decode())

def test_bad_base64_and_large_evidence(w):
    with pytest.raises(DomainError): w.svc.upload_evidence(w.owner,w.company,'bad.txt','not base64')
    with pytest.raises(DomainError): w.svc.upload_evidence(w.owner,w.company,'big.txt',base64.b64encode(b'x'*(2*1024*1024+1)).decode())

def test_evidence_original_hash_dedup_and_pdf_pending(w):
    raw=b'%PDF-1.7\nNot a real return. Parser must not run.'
    a=w.svc.upload_evidence(w.owner,w.company,'sample.pdf',base64.b64encode(raw).decode())
    b=w.svc.upload_evidence(w.owner,w.company,'another.pdf',base64.b64encode(raw).decode())
    assert a['id']==b['id'] and b['deduplicated']
    assert a['extraction_status']=='PDF_STORED_EXTRACTION_PENDING'
    assert w.svc.evidence_content(w.owner,w.company,a['id'])['content']==raw

def test_company_and_role_isolation(w):
    tx=w.tx()
    with pytest.raises(DomainError) as e: w.svc.get_object(w.other_owner,w.other_company,'transaction',tx['id'])
    assert e.value.status==404
    with pytest.raises(DomainError): w.svc.get_object(w.other_owner,w.company,'transaction',tx['id'])
    with pytest.raises(DomainError): w.svc.evidence_content(w.other_owner,w.other_company,w.evidence)
    with pytest.raises(DomainError): w.svc.upload_evidence(w.viewer,w.company,'v.txt',base64.b64encode(b'x').decode())
    assert w.svc.list_objects(w.viewer,w.company,'transaction')[0]['id']==tx['id']

def csv_data(rows):
    out=io.StringIO(); writer=csv.DictWriter(out,fieldnames=list(rows[0])); writer.writeheader();writer.writerows(rows);return out.getvalue()

def test_csv_validation_is_atomic(w):
    a=dict(external_id='csv1',direction='sale',event_date='2026-09-01',counterparty='A',net='100',tax='10',gross='110',tax_category='standard',business_purpose='test',evidence_ids=json.dumps([w.evidence]))
    bad=a|{'external_id':'csv2','gross':'NaN'}
    with pytest.raises(DomainError): w.svc.import_csv(w.owner,w.company,csv_data([a,bad]))
    assert not w.svc.list_objects(w.owner,w.company,'transaction')
    r=w.svc.import_csv(w.owner,w.company,csv_data([a]))
    assert r['created']==1
    assert w.svc.import_csv(w.owner,w.company,csv_data([a]))['deduplicated']==1

def test_csv_late_conflict_rolls_back_entire_batch(w):
    w.tx(external_id='existing')
    a=dict(external_id='new',direction='sale',event_date='2026-09-01',counterparty='A',net='100',tax='10',gross='110')
    with pytest.raises(DomainError): w.svc.import_csv(w.owner,w.company,csv_data([a,a|{'external_id':'existing'}]))
    assert len(w.svc.list_objects(w.owner,w.company,'transaction'))==1

@pytest.mark.parametrize('text',['id,id\n1,2','external_id,direction,event_date,counterparty,net,tax,gross,related_party\na,sale,2026-09-01,A,100,10,110,maybe',''])
def test_csv_bad_headers_and_boolean(w,text):
    with pytest.raises(DomainError): w.svc.import_csv(w.owner,w.company,text)

def test_audit_append_only_and_hashchain(w):
    w.tx()
    r=w.svc.audit_report(w.owner,w.company)
    assert r['integrity']['valid'] and r['integrity']['events']>=3
    with pytest.raises(sqlite3.IntegrityError):
        with w.db.transaction() as c: c.execute('DELETE FROM audit')
    with pytest.raises(sqlite3.IntegrityError):
        with w.db.transaction() as c: c.execute("UPDATE audit SET action='changed'")

def test_audit_detects_tampering_with_db_owner_access(w):
    with w.db.transaction() as c:
        triggers=c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='audit'").fetchall()
        for t in triggers: c.execute('DROP TRIGGER "'+t[0]+'"')
        c.execute("UPDATE audit SET action='tampered' WHERE company_id=?",(w.company,))
    assert not w.svc.audit_report(w.owner,w.company)['integrity']['valid']
