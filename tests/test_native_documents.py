from __future__ import annotations
import hashlib
import subprocess
import sys
from pathlib import Path
import pytest
from price.native_documents import company_document, parse_text_document, pdf_text, parse_pdf_document
from price.db import DomainError, Database
from price.service import Service
from price.ingestion import Ingestion
from price.owner import OwnerView
from price.uploads import Uploads
from test_source_ingestion import upload, BANK
from test_source_workbooks import make_workbook, BANK_ROWS

REGISTRATION='사업자등록증\n법인명(단체명): 주식회사 합성테스트\n등록번호 : 123-45-67890\n대표자: 합성 인물\n'


def simple_pdf(pages=1):
    text=b'BT /F1 12 Tf 40 750 Td (SYNTHETIC PDF SOURCE) Tj ET'
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',
             ('<< /Type /Pages /Count '+str(pages)+' /Kids ['+' '.join(str(5+i)+' 0 R' for i in range(pages))+'] >>').encode(),
             b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
             ('<< /Length '+str(len(text))+' >>\nstream\n').encode()+text+b'\nendstream']
    for i in range(pages): objects.append(b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents 4 0 R >>')
    raw=bytearray(b'%PDF-1.4\n');offsets=[0]
    for index,body in enumerate(objects,1):
        offsets.append(len(raw));raw.extend(str(index).encode()+b' 0 obj\n'+body+b'\nendobj\n')
    xref=len(raw);raw.extend(('xref\n0 '+str(len(objects)+1)+'\n0000000000 65535 f \n').encode())
    for offset in offsets[1:]: raw.extend(f'{offset:010d} 00000 n \n'.encode())
    raw.extend(('trailer\n<< /Size '+str(len(objects)+1)+' /Root 1 0 R >>\nstartxref\n'+str(xref)+'\n%%EOF\n').encode())
    return bytes(raw)


def test_text_company_document_candidates_not_registration_verification():
    result=parse_text_document(REGISTRATION.encode())
    assert result['document_kind']=='company_registration'
    assert result['metadata']['business_number']=='1234567890'
    assert result['metadata']['company_name']=='주식회사 합성테스트'
    assert result['records'][0]['fields']['business_number']['row']==3
    assert result['institution_verified'] is False
    assert 'owner_confirmation' in result['records'][0]['context_pending']


def test_ambiguous_company_fields_not_guessed():
    result=company_document(REGISTRATION+'등록번호: 987-65-43210\n')
    assert result['state']=='VALIDATION_REQUIRED' and not result['records']


def test_missing_company_fields_not_marked_complete():
    result=company_document('사업자등록증\n등록번호: 123-45-67890\n')
    assert result['state']=='VALIDATION_REQUIRED'


def test_native_pdf_text_reader_actual_execution(tmp_path):
    result=pdf_text(simple_pdf(),tmp_path/'reader')
    assert 'SYNTHETIC PDF SOURCE' in result['text']
    assert result['pages']==1 and result['ocr_used'] is False
    assert not list((tmp_path/'reader').iterdir())


def test_unknown_pdf_layout_stays_review(tmp_path):
    result=parse_pdf_document(simple_pdf(),tmp_path/'reader')
    assert result['state']=='FORMAT_REVIEW'
    assert result['issues'][0]['code']=='PDF_TABLE_PROFILE_REQUIRED'


def test_pdf_page_count_prevents_partial_read_claim(tmp_path):
    with pytest.raises(DomainError) as error: pdf_text(simple_pdf(201),tmp_path/'reader')
    assert error.value.code=='PDF_PAGE_LIMIT'


def test_corrupt_pdf_no_internal_output_leak(tmp_path):
    with pytest.raises(DomainError) as error: pdf_text(b'%PDF-1.4\nFAKE PRIVATE CONTENT\n%%EOF',tmp_path/'reader')
    assert error.value.code=='PDF_UNREADABLE'
    assert 'PRIVATE' not in str(error.value)


def test_company_document_becomes_onboarding_candidate_not_financial_case(w):
    b=upload(w,REGISTRATION.encode(),filename='등록증.txt')
    reader=Ingestion(w.svc);result=reader.run_once(w.company)
    assert result['state']=='SUCCEEDED' and result['cases']==0
    owner=OwnerView(w.svc,reader.uploads)
    state=owner.onboarding(w.owner,w.company)
    assert state['company_candidates'][0]['business_number']=='1234567890'
    assert state['needs_confirmation']
    with w.db.connect() as c:
        assert not w.db.list(c,w.company,'journal')
        assert not w.db.list(c,w.company,'ingestion_case')


def test_multisheet_workbook_ingestion_keeps_record_identity(w):
    raw=make_workbook([('은행A',BANK_ROWS),('은행B',BANK_ROWS)])
    b=upload(w,raw,filename='은행목록.xlsx')
    reader=Ingestion(w.svc);result=reader.run_once(w.company)
    assert result['state']=='SUCCEEDED' and result['records']==4 and result['cases']==4
    data=reader.records(w.owner,w.company,b['files'][0]['document']['id'])
    assert len({r['id'] for r in data['rows']})==4


def test_partial_upload_survives_backup_restore(w,tmp_path):
    u=Uploads(w.svc)
    from price.upload_models import UploadBatchInput
    b=u.begin(w.owner,w.company,UploadBatchInput(idempotency_key='restore-upload-01',files=[{'client_file_id':'source1','filename':'통장.csv','size_bytes':len(BANK),'sha256':hashlib.sha256(BANK).hexdigest()}]))
    u.put_chunk(w.owner,w.company,b['id'],'source1',0,BANK,hashlib.sha256(BANK).hexdigest())
    backup=w.db.backup(tmp_path/'backup.sqlite3');target=tmp_path/'restored.sqlite3'
    proc=subprocess.run([sys.executable,'-m','price','restore',backup['path'],str(target),'--sha256',backup['sha256']],capture_output=True,text=True,timeout=10)
    assert proc.returncode==0,proc.stdout
    service=Service(Database(target));restored=Uploads(service)
    assert restored.get(w.owner,w.company,b['id'])['files'][0]['received_bytes']==len(BANK)
    restored.complete(w.owner,w.company,b['id'],b['revision'])
    assert Ingestion(service).run_once(w.company)['cases']==2
    with service.db.connect() as c:
        assert service.db.verify_audit(c,w.company)['valid']
        assert c.execute('SELECT count(*) FROM sessions').fetchone()[0]==0
