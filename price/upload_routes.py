"""Authenticated HTTP contract for owner source uploads."""
from __future__ import annotations
import re
from fastapi import Depends, Request, Query
from .auth import Actor
from .db import DomainError
from .uploads import Uploads
from .ingestion import Ingestion
from .owner import OwnerView, CompanyFactsInput
from .extractions import Extractions, ReprocessInput
from .upload_models import UploadBatchInput, CompleteUploadInput, CancelUploadInput, MAX_CHUNK_BYTES

CHUNK_PATH = re.compile(r'^/api/companies/[A-Za-z0-9_-]+/uploads/[A-Za-z0-9_-]+/files/[A-Za-z0-9_-]+/chunks/[0-9]+$')


def is_chunk_request(method: str, path: str) -> bool:
    return method == 'PUT' and CHUNK_PATH.fullmatch(path) is not None


def register_upload_routes(app, service, actor_dependency):
    uploads = Uploads(service)
    reader = Ingestion(service)
    owner = OwnerView(service, uploads)
    app.state.uploads, app.state.source_reader, app.state.owner = uploads, reader, owner
    prefix = '/api/companies/{company}'

    @app.post(prefix + '/uploads', status_code=201)
    def begin_upload(company: str, data: UploadBatchInput, a: Actor = Depends(actor_dependency)):
        return uploads.begin(a, company, data)

    @app.get(prefix + '/uploads')
    def list_uploads(company: str, a: Actor = Depends(actor_dependency)):
        return uploads.list(a, company)

    @app.get(prefix + '/uploads/{batch}')
    def get_upload(company: str, batch: str, a: Actor = Depends(actor_dependency)):
        return uploads.get(a, company, batch)

    @app.put(prefix + '/uploads/{batch}/files/{file_id}/chunks/{index}')
    async def upload_chunk(company: str, batch: str, file_id: str, index: int, request: Request, a: Actor = Depends(actor_dependency)):
        with service.db.connect() as c:
            service.auth.guard(c, a, company, ('owner', 'preparer', 'reviewer'))
        if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/octet-stream':
            raise DomainError('BINARY_REQUIRED', '파일 조각은 바이너리 전송을 사용해야 합니다.', 415)
        raw = await request.body()
        if len(raw) > MAX_CHUNK_BYTES:
            raise DomainError('CHUNK_SIZE', '파일 조각 크기 제한을 초과했습니다.', 413)
        return uploads.put_chunk(a, company, batch, file_id, index, raw, request.headers.get('x-chunk-sha256', ''))

    @app.post(prefix + '/uploads/{batch}/complete')
    def complete_upload(company: str, batch: str, data: CompleteUploadInput, a: Actor = Depends(actor_dependency)):
        sealed = uploads.complete(a, company, batch, data.expected_revision)
        document_ids = [item['document']['id'] for item in sealed['files'] if item.get('document')]
        reading = []
        for document_id in document_ids:
            for _ in range(2):
                step = reader.run_once(company, document_id=document_id)
                if not step.get('processed'):
                    break
                reading.append({key: step.get(key) for key in ('id', 'document_id', 'state', 'error', 'records', 'cases', 'committed')})
                if step.get('committed'):
                    break
        from .casework import Casework, question_views
        linked = Casework(service).refresh(a, company)
        view = uploads.get(a, company, batch)
        events = Casework(service).list(a, company)
        questions = question_views(events, document_ids)
        states = [item['document']['state'] for item in view['files'] if item.get('document')]
        view['source_reading'] = reading
        view['casework'] = linked
        view['owner_questions'] = questions
        view['reading_pending'] = any(state in ('QUEUED', 'PROCESSING') for state in states) or any(not item.get('document') for item in view['files'])
        view['reading_attention'] = [{'document_id': item['document']['id'], 'state': item['document']['state'], 'issues': item['document'].get('issues') or []} for item in view['files'] if item.get('document') and item['document']['state'] not in ('NORMALIZED', 'QUEUED', 'PROCESSING')]
        view['accounting_complete'] = False
        view['manual_transaction_required'] = False
        return view

    @app.post(prefix + '/uploads/{batch}/cancel')
    def cancel_upload(company: str, batch: str, data: CancelUploadInput, a: Actor = Depends(actor_dependency)):
        return uploads.cancel(a, company, batch, data.expected_revision, data.reason)

    @app.get(prefix + '/documents/{document}')
    def get_document(company: str, document: str, a: Actor = Depends(actor_dependency)):
        return uploads.document(a, company, document)

    @app.get(prefix + '/documents/{document}/extraction')
    def get_extraction(company: str, document: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), a: Actor = Depends(actor_dependency)):
        return reader.records(a, company, document, offset, limit)

    extraction = Extractions(service)
    app.state.extractions = extraction

    @app.post(prefix + '/documents/{document}/reprocess')
    def reprocess_document(company: str, document: str, data: ReprocessInput, a: Actor = Depends(actor_dependency)):
        return extraction.reprocess(a, company, document, data)

    @app.get(prefix + '/documents/{document}/extraction/history')
    def extraction_history(company: str, document: str, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100), a: Actor = Depends(actor_dependency)):
        return extraction.history(a, company, document, offset=offset, limit=limit)

    @app.get(prefix + '/documents/{document}/coverage')
    def extraction_coverage(company: str, document: str, a: Actor = Depends(actor_dependency)):
        return extraction.coverage(a, company, document)

    @app.get(prefix + '/owner/home')
    def owner_home(company: str, a: Actor = Depends(actor_dependency)):
        return owner.home(a, company)

    @app.get(prefix + '/owner/onboarding')
    def owner_onboarding(company: str, a: Actor = Depends(actor_dependency)):
        return owner.onboarding(a, company)

    @app.post(prefix + '/owner/onboarding')
    def owner_confirm(company: str, data: CompanyFactsInput, a: Actor = Depends(actor_dependency)):
        return owner.confirm(a, company, data)

    @app.get(prefix + '/support/formats')
    def supported_formats(company: str, a: Actor = Depends(actor_dependency)):
        with service.db.connect() as c:
            service.auth.guard(c, a, company)
        return {'accepted_institution_formats': [], 'development_readers': [
                    {'id': 'korean-csv-bank', 'kind': 'bank_statement', 'verification': 'synthetic_only'},
                    {'id': 'korean-csv-card', 'kind': 'card_transactions', 'verification': 'synthetic_only'},
                    {'id': 'korean-csv-invoice-list', 'kind': 'tax_invoice_list', 'verification': 'synthetic_only'},
                    {'id': 'korean-csv-purchase-order', 'kind': 'purchase_order', 'verification': 'synthetic_only'},
                    {'id': 'bounded-source-xlsx', 'kind': 'source_workbook', 'verification': 'synthetic_only'},
                    {'id': 'company-text-facts', 'kind': 'company_registration', 'verification': 'synthetic_only'},
                    {'id': 'kec-tax-invoice-facts', 'kind': 'tax_invoice_xml', 'verification': 'synthetic_only', 'signature_verified': False},
                    {'id': 'flat-html-source', 'kind': 'html_xls_tables', 'verification': 'synthetic_only'},
                    {'id': 'pdf-separated-table', 'kind': 'text_pdf_separated_tables', 'verification': 'synthetic_only'}],
                'not_yet_read': ['binary_ole_xls', 'arbitrary_pdf_tables', 'scanned_image', 'encrypted_documents'],
                'limits': {'file_bytes': 50_000_000, 'batch_files': 100, 'batch_bytes': 500_000_000, 'chunk_bytes': MAX_CHUNK_BYTES},
                'notice': '지원 기관의 실제 원본 양식 인수는 아직 완료되지 않았습니다.'}
