"""Apply reviewed, exact-block V01 integration changes inside price only."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
updates = {}


def edit(relative, replacements):
    path = ROOT / relative
    if path.is_symlink() or not path.is_file():
        raise RuntimeError('Expected project source file: ' + relative)
    original = path.read_text(encoding='utf-8')
    content = original
    for before, after in replacements:
        if content.count(before) != 1:
            raise RuntimeError('Exact-block precondition failed: ' + relative)
        content = content.replace(before, after, 1)
    updates[path] = original, content


edit('price/api.py', [
    ('import sqlite3\n', 'import sqlite3\nfrom .upload_routes import is_chunk_request, register_upload_routes\nfrom .upload_models import MAX_CHUNK_BYTES\n'),
    ('        body = bytearray()\n', '        limit = MAX_CHUNK_BYTES if is_chunk_request(scope.get("method", ""), scope.get("path", "")) else self.limit\n        body = bytearray()\n'),
    ('            if len(body) > self.limit:\n', '            if len(body) > limit:\n'),
    ('            if request.url.path.startswith("/api/") and not request.headers.get("content-type", "").startswith("application/json"):\n                return JSONResponse({"code": "JSON_REQUIRED", "message": "JSON 요청만 허용합니다."}, status_code=415)\n',
     '            media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()\n            chunk_route = is_chunk_request(request.method, request.url.path)\n            if request.url.path.startswith("/api/") and media_type != ("application/octet-stream" if chunk_route else "application/json"):\n                return JSONResponse({"code": "BINARY_REQUIRED" if chunk_route else "JSON_REQUIRED", "message": "파일 조각은 바이너리, 나머지 변경은 JSON 요청을 사용하세요."}, status_code=415)\n'),
    ('    prefix = "/api/companies/{company}"\n', '    prefix = "/api/companies/{company}"\n    register_upload_routes(app, svc, actor)\n'),
    ('    def home(): return FileResponse(static / "index.html")\n',
     '    def home(): return FileResponse(static / ("owner.html" if os.getenv("PRICE_OWNER_UI", "1") == "1" else "index.html"))\n\n    @app.get("/expert")\n    def expert_home(): return FileResponse(static / "index.html")\n')
])
edit('price/worker.py', [
    ('from .service import Service, WRITERS\n', 'from .service import Service, WRITERS\nfrom .ingestion import Ingestion\n'),
    ('    q = Queue(Service(Database(args.db)))\n    while True:\n        result = q.run_once()\n',
     '    service = Service(Database(args.db))\n    q, reader = Queue(service), Ingestion(service)\n    turn = 0\n    while True:\n        primary, secondary = (reader, q) if turn % 2 == 0 else (q, reader)\n        turn += 1\n        result = primary.run_once()\n        if not result["processed"]:\n            result = secondary.run_once()\n')
])
edit('price/owner.py', [
    ("    currency: Annotated[StrictStr, Field(pattern=r'^[A-Z]{3}$')] = 'KRW'\n", "    currency: Annotated[StrictStr, Field(pattern=r'^[A-Z]{3}$')] | None = None\n"),
    ('    source_document_id: str | None = None\n', '    source_document_id: Annotated[StrictStr, Field(max_length=160)] | None = None\n')
])
edit('price/static/owner.js', [
    ("currency:x.instrument.currency||'KRW'", 'currency:x.instrument.currency||null'),
    ("    const key='upload_'+await sha256(new TextEncoder().encode(JSON.stringify(manifest)));\n    let batch=await post('/uploads',{idempotency_key:key,files:manifest}); state.upload=batch;\n    if(batch.state==='CANCELLED') throw new Error('같은 파일 묶음의 이전 전송이 취소됐습니다. 운영 확인 후 새로운 전송 번호로 접수해야 합니다.');\n",
     "    const baseKey='upload_'+await sha256(new TextEncoder().encode(JSON.stringify(manifest)));\n    const storageKey='price.transport.'+state.company+'.'+baseKey;\n    let key=sessionStorage.getItem(storageKey)||baseKey;\n    if(state.controller.signal.aborted) throw new DOMException('Paused','AbortError');\n    let batch=await post('/uploads',{idempotency_key:key,files:manifest});\n    if(batch.state==='CANCELLED') { key=baseKey+'_'+crypto.randomUUID(); sessionStorage.setItem(storageKey,key); batch=await post('/uploads',{idempotency_key:key,files:manifest}); }\n    state.upload=batch;\n")
])
edit('price/source_tables.py', [
    ("'작성일자', '작성일', '발급일자'),", "'작성일자', '작성일'),\n    'issued_date': ('발급일자', '발급일시'),\n    'transmitted_date': ('전송일자', '전송일시'),"),
    ("                    elif field == 'date':\n", "                    elif field in ('date', 'issued_date', 'transmitted_date'):\n")
])
edit('tests/test_source_ingestion.py', [
    ("(CARD, 'card_transactions', 2)])\n", "(CARD, 'card_transactions', 2)], ids=['bank', 'invoice-list', 'card'])\n")
])

for path, (before, after) in updates.items():
    if path.read_text(encoding='utf-8') != before:
        raise RuntimeError('Concurrent edit detected; no changes applied')
proof = []
for path, (before, after) in updates.items():
    path.write_text(after, encoding='utf-8')
    proof.append({'path': str(path.relative_to(ROOT)), 'before_sha256': hashlib.sha256(before.encode()).hexdigest(), 'after_sha256': hashlib.sha256(after.encode()).hexdigest()})
out = ROOT / 'evidence' / 'version-loop' / 'v01-integration-changes.json'
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({'files': proof, 'approval_checks_removed': False, 'live_actions_enabled': False}, indent=2), encoding='utf-8')
print(json.dumps({'files_updated': len(proof), 'evidence': str(out.relative_to(ROOT))}))
