from __future__ import annotations
import hmac
import json
import os
import sqlite3
from .upload_routes import is_chunk_request, register_upload_routes
from .upload_models import MAX_CHUNK_BYTES
from pathlib import Path
from urllib.parse import quote, urlparse
from fastapi import FastAPI, Depends, Request
from pydantic import ValidationError
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from .auth import Actor, SESSION_SECONDS, passwordless_login_permitted
from .ai import Advisor
from .db import Database, DomainError, now, uid
from .service import Service, WRITERS
from .filing import Filings
from .worker import Queue
from . import __version__
from .models import (LoginInput, UserInput, EvidenceInput, TransactionInput, FactsInput, JournalInput,
                     RevisionInput, ReverseInput, PeriodInput, CoverageInput, AllocationInput, AuthorityInput,
                     CalculationInput, FilingInput, SubmitInput, PaymentInput, ConsultInput, CSVInput)


class BodyLimit:
    def __init__(self, app, limit=3_200_000): self.app, self.limit = app, limit
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)
        limit = MAX_CHUNK_BYTES if is_chunk_request(scope.get("method", ""), scope.get("path", "")) else self.limit
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect": return
            body.extend(message.get("body", b""))
            if len(body) > limit:
                return await JSONResponse({"code": "BODY_TOO_LARGE", "message": "요청 크기 제한을 초과했습니다."}, status_code=413)(scope, receive, send)
            if not message.get("more_body", False): break
        used = False
        async def replay():
            nonlocal used
            if not used:
                used = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()
        await self.app(scope, replay, send)


def create_app(db_path: str | Path | None = None, advisor: Advisor | None = None) -> FastAPI:
    db = Database(db_path or os.getenv("PRICE_DB", ".data/price.sqlite3"))
    svc = Service(db, advisor)
    filing, queue = Filings(svc), Queue(svc)
    app = FastAPI(title="price · AI 세무사 검증 앱", version=__version__, docs_url=None, redoc_url=None)
    app.state.db, app.state.service, app.state.filings, app.state.queue = db, svc, filing, queue
    app.add_middleware(BodyLimit)
    secure = os.getenv("PRICE_SECURE_COOKIE", "0") == "1"
    allowed_hosts = {"localhost", "127.0.0.1", "::1", "testserver"} | {x.strip() for x in os.getenv("PRICE_ALLOWED_HOSTS", "").split(",") if x.strip()}

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse({"code": exc.code, "message": exc.message, "details": exc.details}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"code": "VALIDATION_ERROR", "message": "입력 형식을 확인하세요.", "details": [{"field": list(e["loc"]), "message": e["msg"]} for e in exc.errors()]}, status_code=422)

    @app.exception_handler(sqlite3.IntegrityError)
    async def conflict(request, exc):
        return JSONResponse({"code": "DATABASE_CONSTRAINT", "message": "중복 또는 변경 불가 데이터입니다."}, status_code=409)

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.url.hostname not in allowed_hosts:
            return JSONResponse({"code": "HOST_NOT_ALLOWED", "message": "허용되지 않은 호스트입니다."}, status_code=400)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
                return JSONResponse({"code": "ORIGIN_REJECTED", "message": "다른 사이트에서 보낸 변경 요청입니다."}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"code": "ORIGIN_REJECTED", "message": "교차 사이트 요청이 차단되었습니다."}, status_code=403)
            media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            chunk_route = is_chunk_request(request.method, request.url.path)
            if request.url.path.startswith("/api/") and media_type != ("application/octet-stream" if chunk_route else "application/json"):
                return JSONResponse({"code": "BINARY_REQUIRED" if chunk_route else "JSON_REQUIRED", "message": "파일 조각은 바이너리, 나머지 변경은 JSON 요청을 사용하세요."}, status_code=415)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    def actor(request: Request):
        a = svc.auth.session(request.cookies.get("price_session"))
        if request.method not in ("GET", "HEAD", "OPTIONS") and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), a.csrf):
            raise DomainError("CSRF_REJECTED", "세션 확인 토큰이 맞지 않습니다.", 403)
        return a

    @app.get("/health")
    def health(): return {"ok": True, "project": "price", "worker": "price", "product": "AI 세무사", "version": __version__, "mode": "validation", "live_filing": False}

    def session_response(token: str, actor: Actor) -> JSONResponse:
        response = JSONResponse({"username": actor.username, "csrf": actor.csrf, "companies": svc.auth.companies(actor)})
        response.set_cookie("price_session", token, max_age=SESSION_SECONDS, httponly=True, secure=secure, samesite="strict", path="/")
        return response

    @app.post("/api/login")
    async def login(request: Request):
        server = request.scope.get("server") or ("", None)
        server_host = server[0] if isinstance(server, (list, tuple)) and server else ""
        if passwordless_login_permitted(request.url.hostname, str(server_host)):
            token, actor = svc.auth.issue_sole_owner_session()
            return session_response(token, actor)
        try:
            payload = json.loads(await request.body() or b"null")
        except json.JSONDecodeError:
            return JSONResponse({"code": "VALIDATION_ERROR", "message": "입력 형식을 확인하세요."}, status_code=422)
        try:
            data = LoginInput.model_validate(payload)
        except ValidationError as exc:
            return JSONResponse({
                "code": "VALIDATION_ERROR",
                "message": "입력 형식을 확인하세요.",
                "details": [{"field": list(err["loc"]), "message": err["msg"]} for err in exc.errors()],
            }, status_code=422)
        token, actor = svc.auth.login(data.username, data.password)
        return session_response(token, actor)

    @app.get("/api/session")
    def session(a: Actor = Depends(actor)): return {"username": a.username, "csrf": a.csrf, "companies": svc.auth.companies(a)}

    @app.post("/api/logout")
    def logout(request: Request, a: Actor = Depends(actor)):
        svc.auth.logout(request.cookies["price_session"])
        response = JSONResponse({"ok": True}); response.delete_cookie("price_session", path="/")
        return response

    prefix = "/api/companies/{company}"
    register_upload_routes(app, svc, actor)
    from .casework_routes import register_casework_routes
    register_casework_routes(app, svc, actor)
    from .policy_routes import register_policy_routes
    register_policy_routes(app, svc, actor)
    from .automation_routes import register_automation_routes
    register_automation_routes(app, svc, actor)
    from .operations_routes import register_operations_routes
    register_operations_routes(app, svc, actor)
    from .bookkeeping_routes import register_bookkeeping_routes
    register_bookkeeping_routes(app, svc, actor)
    from .inventory_routes import register_inventory_routes
    register_inventory_routes(app, svc, actor)
    from .payment_routes import register_payment_routes
    from .tax_return_routes import register_tax_return_routes
    register_payment_routes(app, svc, actor)
    register_tax_return_routes(app, svc, actor)
    from .compensation_routes import register_compensation_routes
    register_compensation_routes(app, svc, actor)
    from .cashflow_routes import register_cashflow_routes
    register_cashflow_routes(app, svc, actor)

    @app.get(prefix + "/overview")
    def overview(company: str, a: Actor = Depends(actor)): return svc.overview(a, company)

    @app.get(prefix + "/capabilities")
    def capabilities(company: str, a: Actor = Depends(actor)):
        with db.connect() as c: svc.auth.guard(c, a, company)
        return {"ledger": "validation_sqlite", "imports": ["manual", "csv", "original_document_storage"],
                "llm": "ollama_configured_unverified" if svc.advisor.configured else "not_configured",
                "vat": "posted_ledger_review_aggregation", "corporate": "rulepack_not_verified", "withholding": "rulepack_not_verified",
                "submission": "local_simulator_only", "bank": "not_connected", "hometax": "not_connected", "ocr": "not_implemented",
                "worker": "durable_proposal_queue", "remote_worker_registration": "not_verified"}

    @app.post(prefix + "/users", status_code=201)
    def users(company: str, data: UserInput, a: Actor = Depends(actor)): return svc.add_user(a, company, data)

    @app.get(prefix + "/evidence")
    def evidence(company: str, a: Actor = Depends(actor)): return svc.evidence_list(a, company)

    @app.post(prefix + "/evidence", status_code=201)
    def upload(company: str, data: EvidenceInput, a: Actor = Depends(actor)): return svc.upload_evidence(a, company, data.filename, data.content_base64)

    @app.get(prefix + "/evidence/{ident}/download")
    def download(company: str, ident: str, a: Actor = Depends(actor)):
        r = svc.evidence_content(a, company, ident)
        return Response(r["content"], media_type=r["mime"], headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(r["filename"], safe="")})

    @app.post(prefix + "/transactions", status_code=201)
    def transaction(company: str, data: TransactionInput, a: Actor = Depends(actor)): return svc.create_transaction(a, company, data)

    @app.post(prefix + "/imports/csv")
    def csv_import(company: str, data: CSVInput, a: Actor = Depends(actor)): return svc.import_csv(a, company, data.csv_text)

    @app.post(prefix + "/transactions/{ident}/facts")
    def facts(company: str, ident: str, data: FactsInput, a: Actor = Depends(actor)): return svc.update_facts(a, company, ident, data)

    @app.post(prefix + "/transactions/{ident}/analyze")
    def analyze(company: str, ident: str, a: Actor = Depends(actor)): return svc.analyze(a, company, ident)

    @app.post(prefix + "/transactions/{ident}/enqueue")
    def enqueue(company: str, ident: str, a: Actor = Depends(actor)): return queue.enqueue(a, company, ident)

    @app.get(prefix + "/jobs")
    def jobs(company: str, a: Actor = Depends(actor)): return queue.list(a, company)

    @app.post(prefix + "/worker/run")
    def run_worker(company: str, a: Actor = Depends(actor)):
        with db.connect() as c: svc.auth.guard(c, a, company, WRITERS)
        return queue.run_once(company)

    @app.post(prefix + "/journals", status_code=201)
    def create_journal(company: str, data: JournalInput, a: Actor = Depends(actor)): return svc.create_journal(a, company, data)

    @app.post(prefix + "/journals/{ident}/reverse")
    def reverse(company: str, ident: str, data: ReverseInput, a: Actor = Depends(actor)):
        return svc.reverse_journal(a, company, ident, data.event_date.isoformat(), data.reason, data.expected_revision)

    @app.post(prefix + "/journals/{ident}/{action}")
    def journal_action(company: str, ident: str, action: str, data: RevisionInput, a: Actor = Depends(actor)):
        return svc.transition_journal(a, company, ident, action, data.expected_revision, data.reason)

    @app.post(prefix + "/allocations", status_code=201)
    def allocate(company: str, data: AllocationInput, a: Actor = Depends(actor)): return svc.allocate(a, company, data)

    @app.get(prefix + "/trial-balance")
    def trial(company: str, a: Actor = Depends(actor)): return svc.trial_balance(a, company)

    @app.post(prefix + "/periods", status_code=201)
    def period(company: str, data: PeriodInput, a: Actor = Depends(actor)): return svc.create_period(a, company, data)

    @app.get(prefix + "/periods/{ident}/readiness")
    def readiness(company: str, ident: str, a: Actor = Depends(actor)): return svc.readiness(a, company, ident)

    @app.post(prefix + "/periods/{ident}/coverage")
    def coverage(company: str, ident: str, data: CoverageInput, a: Actor = Depends(actor)): return svc.coverage(a, company, ident, data)

    @app.post(prefix + "/periods/{ident}/{action}")
    def period_action(company: str, ident: str, action: str, data: RevisionInput, a: Actor = Depends(actor)):
        return svc.period_action(a, company, ident, action, data.expected_revision, data.reason)

    @app.post(prefix + "/authorities", status_code=201)
    def authority(company: str, data: AuthorityInput, a: Actor = Depends(actor)): return svc.add_authority(a, company, data)

    @app.post(prefix + "/authorities/{ident}/review")
    def authority_review(company: str, ident: str, data: RevisionInput, a: Actor = Depends(actor)): return svc.review_authority(a, company, ident, data.expected_revision, data.reason)

    @app.post(prefix + "/calculations", status_code=201)
    def calculation(company: str, data: CalculationInput, a: Actor = Depends(actor)): return svc.calculate(a, company, data)

    @app.post(prefix + "/filings", status_code=201)
    def draft_filing(company: str, data: FilingInput, a: Actor = Depends(actor)): return filing.create(a, company, data)

    @app.post(prefix + "/filings/{ident}/approve")
    def approve_filing(company: str, ident: str, data: RevisionInput, a: Actor = Depends(actor)): return filing.approve(a, company, ident, data.expected_revision, data.reason)

    @app.post(prefix + "/filings/{ident}/submit")
    def submit(company: str, ident: str, data: SubmitInput, a: Actor = Depends(actor)): return filing.submit(a, company, ident, data)

    @app.post(prefix + "/filings/{ident}/reconcile")
    def reconcile(company: str, ident: str, a: Actor = Depends(actor)): return filing.reconcile(a, company, ident)

    @app.post(prefix + "/filings/{ident}/payments", status_code=201)
    def payment(company: str, ident: str, data: PaymentInput, a: Actor = Depends(actor)): return filing.record_payment(a, company, ident, data)

    @app.post(prefix + "/consultations", status_code=201)
    def consult(company: str, data: ConsultInput, a: Actor = Depends(actor)): return svc.consult(a, company, data)

    @app.get(prefix + "/audit")
    def audit(company: str, a: Actor = Depends(actor)): return svc.audit_report(a, company)

    @app.get(prefix + "/export")
    def export(company: str, a: Actor = Depends(actor)):
        from .service import PUBLIC_KINDS
        with db.transaction() as c:
            svc.auth.guard(c, a, company)
            db.audit(c, company, a.id, "workspace.exported", company)
            data = {"schema": "price.review-export/1", "company": company, "generated_at": now(), "official_form": False,
                    "objects": {k: db.list(c, company, k) for k in sorted(PUBLIC_KINDS)}, "audit_integrity": db.verify_audit(c, company)}
        return Response(json.dumps(data, ensure_ascii=False, indent=2), media_type="application/json", headers={"Content-Disposition": "attachment; filename=price-review-export.json"})

    names = {"transactions": "transaction", "cases": "case", "journals": "journal", "periods": "period", "allocations": "allocation", "authorities": "authority", "calculations": "calculation", "filings": "filing", "payments": "payment", "consultations": "consultation", "agent-runs": "agent_run", "submissions": "submission"}

    @app.get(prefix + "/{collection}/{ident}/history")
    def history(company: str, collection: str, ident: str, a: Actor = Depends(actor)):
        if collection not in names: raise DomainError("NOT_FOUND", "대상이 없습니다.", 404)
        return svc.object_history(a, company, names[collection], ident)

    @app.get(prefix + "/{collection}")
    def list_collection(company: str, collection: str, a: Actor = Depends(actor)):
        if collection not in names: raise DomainError("NOT_FOUND", "목록이 없습니다.", 404)
        return svc.list_objects(a, company, names[collection])

    @app.get(prefix + "/{collection}/{ident}")
    def get_collection(company: str, collection: str, ident: str, a: Actor = Depends(actor)):
        if collection not in names: raise DomainError("NOT_FOUND", "대상이 없습니다.", 404)
        return svc.get_object(a, company, names[collection], ident)

    static = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def home(): return FileResponse(static / ("owner.html" if os.getenv("PRICE_OWNER_UI", "1") == "1" else "index.html"))

    @app.get("/expert")
    def expert_home(): return FileResponse(static / "index.html")

    @app.get("/cashflow")
    def cashflow_workbench(): return FileResponse(static / "cashflow.html")

    @app.get('/bookkeeping')
    def bookkeeping_home(): return FileResponse(static / 'bookkeeping.html')

    return app
