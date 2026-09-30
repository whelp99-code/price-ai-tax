"""Resource-limited text-layer PDF reader and factual company-document parser.

Native parsing is a restricted child process, not an antivirus guarantee or
an OCR service. No document action, JavaScript, URL, or attachment is executed.
"""
from __future__ import annotations
import os
import re
import resource
import subprocess
import tempfile
from pathlib import Path
from .db import DomainError
from .ingest_safety import text_decode

MAX_PAGES = 200
MAX_OUTPUT = 8_000_000


def _limits():
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 * 1024, 384 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT, MAX_OUTPUT))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def pdf_text(raw: bytes, work_root: Path) -> dict:
    tools = {name: Path('/usr/bin') / name for name in ('pdfinfo', 'pdftotext')}
    if any(not path.is_file() for path in tools.values()):
        raise DomainError('PDF_READER_UNAVAILABLE', 'PDF 읽기 도구 연결이 필요합니다.')
    work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if work_root.is_symlink():
        raise DomainError('READER_WORKSPACE_INVALID', '자료 읽기 작업공간을 확인해야 합니다.')
    with tempfile.TemporaryDirectory(prefix='pdf-', dir=work_root) as folder:
        folder = Path(folder)
        source, output, info = folder/'source.pdf', folder/'text.txt', folder/'info.txt'
        source.write_bytes(raw); source.chmod(0o600)
        env = {'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','LC_ALL':'C.UTF-8','HOME':str(folder)}
        try:
            with info.open('xb') as stdout:
                inspection = subprocess.run([str(tools['pdfinfo']),str(source)],stdin=subprocess.DEVNULL,stdout=stdout,stderr=subprocess.DEVNULL,
                                            timeout=8,cwd=folder,env=env,preexec_fn=_limits,start_new_session=True,check=False)
            if inspection.returncode != 0 or info.stat().st_size > MAX_OUTPUT:
                raise DomainError('PDF_UNREADABLE', '손상되었거나 추가 확인이 필요한 PDF입니다.')
            description = info.read_text(encoding='utf-8',errors='strict')
            counts = re.findall(r'^Pages:\s+([0-9]+)\s*$',description,re.M)
            if len(counts) != 1 or not 1 <= int(counts[0]) <= MAX_PAGES:
                raise DomainError('PDF_PAGE_LIMIT', '지원하는 PDF 페이지 수를 확인해야 합니다.')
            if re.search(r'^Encrypted:\s+yes\b',description,re.M):
                raise DomainError('PASSWORD_REQUIRED', '암호가 있는 PDF는 별도 확인이 필요합니다.')
            parsed = subprocess.run([str(tools['pdftotext']),'-layout','-enc','UTF-8',str(source),str(output)],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,timeout=8,cwd=folder,env=env,preexec_fn=_limits,start_new_session=True,check=False)
            if parsed.returncode != 0 or not output.is_file() or output.stat().st_size > MAX_OUTPUT:
                raise DomainError('PDF_TEXT_FAILED', 'PDF의 글자를 안전하게 읽지 못했습니다.')
            text = output.read_text(encoding='utf-8',errors='strict')
        except subprocess.TimeoutExpired:
            raise DomainError('PDF_READER_TIMEOUT', 'PDF 읽기 제한시간을 초과했습니다. 원본은 보존합니다.')
        except (OSError, UnicodeError):
            raise DomainError('PDF_READER_FAILED', 'PDF 읽기에 실패했습니다. 원본을 확인해야 합니다.')
        pages = text.split('\f')
        if pages and not pages[-1].strip():
            pages.pop()
        if not any(page.strip() for page in pages):
            raise DomainError('IMAGE_READER_REQUIRED', '텍스트가 없는 스캔 자료입니다. 이미지 인식 연결이 필요합니다.')
        return {'text':text,'pages':int(counts[0]),'method':'poppler-text-layer','ocr_used':False,
                'resource_limits':{'cpu_seconds':5,'memory_mb':384,'output_bytes':MAX_OUTPUT,'pages':MAX_PAGES}}


def company_document(text: str, source_format='text') -> dict | None:
    if not re.search(r'사업자\s*등록\s*증',text):
        return None
    numbers = list(dict.fromkeys(re.findall(r'(?:사업자\s*)?등록\s*번호\s*[:：]?\s*([0-9]{3}-?[0-9]{2}-?[0-9]{5})(?![0-9])',text)))
    names = list(dict.fromkeys(x.strip() for x in re.findall(r'(?:법인\s*명\s*(?:\(단체명\))?|상\s*호|법인명칭)\s*[:：]\s*([^\r\n\f]{1,120})',text)))
    if len(numbers) != 1 or len(names) != 1:
        return {'state':'VALIDATION_REQUIRED','document_kind':'company_registration','records':[],
                'issues':[{'row':0,'code':'COMPANY_DOCUMENT_FIELDS_AMBIGUOUS'}],'record_count':0,
                'parser_version':'company-text-facts/1','institution_verified':False,'accounting_complete':False}
    number = numbers[0].replace('-','')
    fields={}
    for key,value in (('business_number',numbers[0]),('company_name',names[0])):
        index=text.index(value)
        page=text[:index].count('\f')+1
        page_text=text[:index].rsplit('\f',1)[-1]
        fields[key]={'raw':value,'row':page_text.count('\n')+1,'page':page,'source_format':source_format}
    record={'row':1,'kind':'company_registration','values':{'company_name':names[0],'business_number':number},
            'fields':fields,'context_pending':['owner_confirmation','legal_registry_validation']}
    return {'state':'NORMALIZED','document_kind':'company_registration','records':[record],
            'issues':[],'record_count':1,'source_rows':1,'metadata':{'company_name':names[0],'business_number':number},
            'parser_version':'company-text-facts/1','institution_verified':False,'accounting_complete':False}


def parse_text_document(raw: bytes) -> dict:
    from .source_tables import parse_source_table
    text, encoding = text_decode(raw)
    company = company_document(text)
    return {**company,'encoding':encoding} if company else parse_source_table(raw)


def parse_pdf_document(raw: bytes, work_root: Path) -> dict:
    extracted=pdf_text(raw,work_root)
    company=company_document(extracted['text'],'pdf')
    if company:
        return {**company,'extraction_method':extracted['method'],'page_count':extracted['pages'],'ocr_used':False}
    from .source_pdf_tables import parse_pdf_table_text
    table = parse_pdf_table_text(extracted['text'])
    if table['state'] != 'FORMAT_REVIEW':
        return {**table, 'page_count': extracted['pages'], 'ocr_used': False}
    return {'state':'FORMAT_REVIEW','document_kind':'pdf_text_document','records':[],
            'issues':[{'row':0,'code':'PDF_TABLE_PROFILE_REQUIRED'}],'parser_version':'pdf-text-layer/1',
            'page_count':extracted['pages'],'extraction_method':extracted['method'],'ocr_used':False,
            'record_count':0,'accounting_complete':False,'institution_verified':False}
