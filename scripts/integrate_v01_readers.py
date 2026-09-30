"""Integrate tested initial workbook/company-document readers without widening execution rights."""
from pathlib import Path
root=Path(__file__).resolve().parents[1]
pending={}

def edit(name,changes):
    path=root/name
    original=path.read_text(encoding='utf-8');content=original
    for before,after in changes:
        if content.count(before)!=1: raise RuntimeError('Exact source check failed: '+name)
        content=content.replace(before,after,1)
    pending[path]=(original,content)

edit('price/ingest_safety.py',[
    ('import zipfile\n','import zipfile\nimport zlib\n'),
    ('zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as exc:', 'zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError, OSError) as exc:')
])
edit('price/uploads.py',[
    ("                    state = 'QUEUED' if scan['state'] == 'STRUCTURE_CHECKED' else scan['state']\n",
     "                    initial_reader = scan['state'] == 'STRUCTURE_CHECKED' or (scan['state'] == 'READER_REQUIRED' and scan['code'] in ('TABLE_READER_REQUIRED', 'DOCUMENT_READER_REQUIRED'))\n                    state = 'QUEUED' if initial_reader else scan['state']\n")
])
edit('price/ingestion.py',[
    ('from .source_tables import parse_source_table\n','from .source_tables import parse_source_table\nfrom .source_workbooks import parse_source_workbook\nfrom .native_documents import parse_text_document, parse_pdf_document\n'),
    ("ident = 'source_' + digest({'document': document['id'], 'parser': result.get('parser_version'), 'row': record['row']})",
     "ident = 'source_' + digest({'document': document['id'], 'parser': result.get('parser_version'), 'row': record['row'], **({'sheet': record['sheet']} if record.get('sheet') else {})})"),
    ("                if result['state'] == 'NORMALIZED':\n", "                if result['state'] == 'NORMALIZED' and record['kind'] != 'company_registration':\n"),
    ("            if row['mime'] in ('text/csv', 'text/plain'):\n                result = parse_source_table(row['content'])\n            else:\n",
     "            if row['mime'] in ('text/csv', 'text/plain'):\n                result = parse_text_document(row['content'])\n            elif row['mime'] == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':\n                result = parse_source_workbook(row['content'])\n            elif row['mime'] == 'application/pdf':\n                result = parse_pdf_document(row['content'], self.db.path.parent / 'reader-work')\n            else:\n"),
    ("ORDER BY CAST(json_extract(body,'$.row') AS INTEGER) LIMIT ? OFFSET ?", "ORDER BY COALESCE(json_extract(body,'$.sheet'),''),CAST(json_extract(body,'$.row') AS INTEGER) LIMIT ? OFFSET ?")
])
edit('price/owner.py',[
    ('            candidates = []\n', '            candidates, company_candidates = [], []\n'),
    ("                info = json.loads(doc['result']).get('metadata', {})\n",
     "                info = json.loads(doc['result']).get('metadata', {})\n                if info.get('company_name') and info.get('business_number'):\n                    company_candidates.append({'name':info['company_name'],'business_number':info['business_number'],'source_document_id':doc['id'],'legal_registry_verified':False})\n"),
    ("                    'instrument_candidates': candidates, 'needs_confirmation': not bool(current),\n",
     "                    'instrument_candidates': candidates, 'company_candidates': company_candidates, 'needs_confirmation': not bool(current),\n")
])
edit('price/source_tables.py',[
    ("                    elif field in ('date', 'issued_date', 'transmitted_date'):\n                        values[field] = date_value(value)\n",
     "                    elif field in ('date', 'issued_date', 'transmitted_date'):\n                        values[field] = date_value(value) if value or field == 'date' else None\n")
])
edit('price/static/owner.js',[
    ("tax_invoice_list:'세금계산서 목록', unknown:'자료 종류 확인 필요'", "tax_invoice_list:'세금계산서 목록', company_registration:'사업자등록증', mixed_workbook:'여러 종류의 표', unknown:'자료 종류 확인 필요'"),
    ("  const name = field('회사 이름','name',current.facts?.name || current.profile_name,true);\n  const number = field('사업자등록번호 (자료에 없으면 비워두세요)','business_number',current.facts?.business_number || '');\n",
     "  const candidates=current.company_candidates||[];\n  const unique=new Set(candidates.map(x=>x.name+'|'+x.business_number));\n  const candidate=unique.size===1?candidates[0]:null;\n  if(candidate&&!current.facts)root.append(element('p','사업자등록증에서 읽은 회사 정보입니다. 사실과 맞는지 확인해 주세요.','note'));\n  if(unique.size>1)root.append(element('p','서로 다른 회사 정보가 있는 자료를 발견했습니다. 이 자료함의 회사 정보를 확인해 주세요.','note'));\n  const name = field('회사 이름','name',current.facts?.name || candidate?.name || current.profile_name,true);\n  const number = field('사업자등록번호 (자료에 없으면 비워두세요)','business_number',current.facts?.business_number || candidate?.business_number || '');\n"),
    ("mode:current.mode,instruments:checks", "mode:current.mode,source_document_id:candidate?.source_document_id||null,instruments:checks"),
])
# Do not invent table readers for arbitrary PDFs, scans, encrypted documents or old XLS.
edit('price/static/owner.html',[
    ('현재 자동 읽기: 한글 표 형식 CSV/TSV. 나머지 형식은 읽기 지원 준비 상태로 구분합니다.',
     '현재 자동 읽기: 한글 표 CSV/TSV·XLSX, 텍스트형 사업자등록증. 스캔·일반 PDF 표·구형 XLS는 확인 대기로 구분합니다.')
])
edit('price/upload_routes.py',[
    ("{'id': 'korean-csv-invoice-list', 'kind': 'tax_invoice_list', 'verification': 'synthetic_only'}],",
     "{'id': 'korean-csv-invoice-list', 'kind': 'tax_invoice_list', 'verification': 'synthetic_only'},\n                    {'id': 'bounded-source-xlsx', 'kind': 'source_workbook', 'verification': 'synthetic_only'},\n                    {'id': 'company-text-facts', 'kind': 'company_registration', 'verification': 'synthetic_only'}],"),
    ("'not_yet_read': ['xls', 'xlsx', 'pdf', 'scanned_image', 'tax_invoice_xml'],", "'not_yet_read': ['xls', 'arbitrary_pdf_tables', 'scanned_image', 'tax_invoice_xml', 'encrypted_documents'],")
])
# The no-op guard above verifies the relevant UI text remains present without changing it.
for path,(before,after) in pending.items():
    if path.read_text(encoding='utf-8')!=before: raise RuntimeError('Concurrent change; no changes applied')
for path,(before,after) in pending.items(): path.write_text(after,encoding='utf-8')
print('Initial source readers integrated:',len(pending),'files; no posting or filing permissions changed.')
