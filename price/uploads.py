"""Company-scoped resumable source upload and transactional ingestion outbox.

Original documents are immutable blobs; upload chunks are temporary transport
fragments. No journal approval, posting or external execution happens here.
"""
from __future__ import annotations
import hashlib
import json
import re
from .db import DomainError, canonical, digest, now, uid
from .service import WRITERS
from .upload_models import UploadBatchInput, UploadFileInput, MAX_CHUNK_BYTES
from .ingest_safety import inspect_document

SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS upload_schema_version(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO upload_schema_version VALUES(1);
CREATE TABLE IF NOT EXISTS upload_batches(
 company_id TEXT NOT NULL REFERENCES companies(id), id TEXT NOT NULL,
 submitted_by TEXT NOT NULL, idempotency_key TEXT NOT NULL, manifest_hash TEXT NOT NULL,
 declared_bytes INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('OPEN','SEALED','CANCELLED')),
 revision INTEGER NOT NULL DEFAULT 1, reason TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(company_id,id), UNIQUE(company_id,idempotency_key));
CREATE TABLE IF NOT EXISTS upload_documents(
 company_id TEXT NOT NULL REFERENCES companies(id), id TEXT NOT NULL,
 sha256 TEXT NOT NULL, blob_id TEXT NOT NULL, filename TEXT NOT NULL, size_bytes INTEGER NOT NULL,
 mime TEXT NOT NULL, inspection TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 result TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(company_id,id), UNIQUE(company_id,sha256),
 FOREIGN KEY(company_id,blob_id) REFERENCES blobs(company_id,id));
CREATE TABLE IF NOT EXISTS upload_files(
 company_id TEXT NOT NULL, batch_id TEXT NOT NULL, id TEXT NOT NULL, manifest TEXT NOT NULL,
 document_id TEXT, PRIMARY KEY(company_id,batch_id,id),
 FOREIGN KEY(company_id,batch_id) REFERENCES upload_batches(company_id,id),
 FOREIGN KEY(company_id,document_id) REFERENCES upload_documents(company_id,id));
CREATE TABLE IF NOT EXISTS upload_chunks(
 company_id TEXT NOT NULL, batch_id TEXT NOT NULL, file_id TEXT NOT NULL,
 chunk_index INTEGER NOT NULL CHECK(chunk_index>=0), sha256 TEXT NOT NULL,
 content BLOB NOT NULL CHECK(length(content)>0 AND length(content)<=1000000),
 PRIMARY KEY(company_id,batch_id,file_id,chunk_index),
 FOREIGN KEY(company_id,batch_id,file_id) REFERENCES upload_files(company_id,batch_id,id));
CREATE TABLE IF NOT EXISTS upload_jobs(
 id TEXT PRIMARY KEY, company_id TEXT NOT NULL, document_id TEXT NOT NULL, requested_by TEXT NOT NULL,
 state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 lease_token TEXT, lease_until INTEGER NOT NULL DEFAULT 0, error TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(company_id,document_id),
 FOREIGN KEY(company_id,document_id) REFERENCES upload_documents(company_id,id));
CREATE INDEX IF NOT EXISTS upload_jobs_pending ON upload_jobs(state,lease_until,created_at);
CREATE TRIGGER IF NOT EXISTS extraction_snapshot_no_update BEFORE UPDATE ON objects WHEN OLD.kind IN ('extraction_run','extraction_request') BEGIN SELECT RAISE(ABORT,'EXTRACTION_HISTORY_IMMUTABLE'); END;
CREATE TRIGGER IF NOT EXISTS extraction_snapshot_no_delete BEFORE DELETE ON objects WHEN OLD.kind IN ('extraction_run','extraction_request') BEGIN SELECT RAISE(ABORT,'EXTRACTION_HISTORY_IMMUTABLE'); END;
COMMIT;
"""


class Uploads:
    def __init__(self, service):
        self.service, self.db, self.auth = service, service.db, service.auth
        with self.db.connect() as c:
            exists = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='upload_schema_version'").fetchone()
            if exists and c.execute('SELECT max(version) FROM upload_schema_version').fetchone()[0] != 1:
                raise DomainError('UPLOAD_SCHEMA_UNSUPPORTED', '자료 처리 저장소의 버전을 확인해야 합니다.')
            c.executescript(SCHEMA)

    @staticmethod
    def _batch(c, company, ident):
        row = c.execute('SELECT * FROM upload_batches WHERE company_id=? AND id=?', (company, ident)).fetchone()
        if not row:
            raise DomainError('NOT_FOUND', '자료 묶음을 찾을 수 없습니다.', 404)
        return dict(row)

    @staticmethod
    def _revision(batch, expected):
        if type(expected) is not int or batch['revision'] != expected:
            raise DomainError('STALE_VERSION', '자료 상태가 바뀌었습니다. 현재 상태를 다시 불러오세요.')

    def _view(self, c, company, ident):
        batch = self._batch(c, company, ident)
        files = []
        for row in c.execute('SELECT * FROM upload_files WHERE company_id=? AND batch_id=? ORDER BY id', (company, ident)):
            item = UploadFileInput.model_validate(json.loads(row['manifest']))
            chunks = [dict(x) for x in c.execute('SELECT chunk_index,sha256,length(content) AS size_bytes FROM upload_chunks WHERE company_id=? AND batch_id=? AND file_id=? ORDER BY chunk_index', (company, ident, row['id']))]
            document = None
            if row['document_id']:
                d = c.execute('SELECT * FROM upload_documents WHERE company_id=? AND id=?', (company, row['document_id'])).fetchone()
                result = json.loads(d['result']) if d['result'] else {}
                document = {'id': d['id'], 'blob_id': d['blob_id'], 'state': d['state'],
                            'inspection': json.loads(d['inspection']), 'record_count': result.get('record_count', 0),
                            'document_kind': result.get('document_kind'), 'issues': result.get('issues', []),
                            'accounting_complete': False}
            files.append({**item.model_dump(), 'chunk_count': item.chunk_count, 'received_chunks': chunks,
                          'received_bytes': item.size_bytes if document else sum(x['size_bytes'] for x in chunks),
                          'document': document})
        states = [x['document']['state'] for x in files if x['document']]
        status = batch['state']
        if batch['state'] == 'SEALED':
            status = 'READING' if any(s in ('QUEUED', 'PROCESSING') for s in states) else 'READ_COMPLETE_ACCOUNTING_PENDING' if states and all(s == 'NORMALIZED' for s in states) else 'NEEDS_ATTENTION'
        return {'id': ident, 'revision': batch['revision'], 'state': batch['state'], 'processing_status': status,
                'created_at': batch['created_at'], 'files': files, 'accounting_complete': False,
                'notice': '파일 읽기와 거래 자료 확인 단계입니다. 장부·세금 처리는 별도로 검증합니다.'}

    def begin(self, actor, company, data: UploadBatchInput):
        data = UploadBatchInput.model_validate(data)
        body_hash = digest(data.model_dump())
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, WRITERS)
            previous = c.execute('SELECT id,manifest_hash FROM upload_batches WHERE company_id=? AND idempotency_key=?', (company, data.idempotency_key)).fetchone()
            if previous:
                if previous['manifest_hash'] != body_hash:
                    raise DomainError('UPLOAD_IDEMPOTENCY_CONFLICT', '같은 전송 번호에 다른 자료가 있습니다.')
                return {**self._view(c, company, previous['id']), 'reused': True}
            pending = c.execute("SELECT count(*),COALESCE(sum(declared_bytes),0) FROM upload_batches WHERE company_id=? AND state='OPEN'", (company,)).fetchone()
            stored = c.execute('SELECT COALESCE(sum(length(content)),0) FROM blobs WHERE company_id=?', (company,)).fetchone()[0]
            declared = sum(x.size_bytes for x in data.files)
            if pending[0] >= 20 or pending[1] + stored + declared > 2_000_000_000:
                raise DomainError('UPLOAD_QUOTA', '자료함 용량 또는 진행 중인 전송 한도를 확인해야 합니다.', 413)
            ident, ts = uid('batch'), now()
            c.execute('INSERT INTO upload_batches(company_id,id,submitted_by,idempotency_key,manifest_hash,declared_bytes,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                      (company, ident, actor.id, data.idempotency_key, body_hash, declared, 'OPEN', ts, ts))
            for item in data.files:
                c.execute('INSERT INTO upload_files(company_id,batch_id,id,manifest) VALUES(?,?,?,?)', (company, ident, item.client_file_id, canonical(item.model_dump())))
            self.db.audit(c, company, actor.id, 'upload.started', ident, {'files': len(data.files), 'bytes': declared})
            return {**self._view(c, company, ident), 'reused': False}

    def get(self, actor, company, ident):
        with self.db.connect() as c:
            self.auth.guard(c, actor, company)
            return self._view(c, company, ident)

    def list(self, actor, company):
        with self.db.connect() as c:
            self.auth.guard(c, actor, company)
            ids = [r[0] for r in c.execute('SELECT id FROM upload_batches WHERE company_id=? ORDER BY created_at DESC LIMIT 100', (company,))]
            return [self._view(c, company, ident) for ident in ids]

    def put_chunk(self, actor, company, batch_id, file_id, index, content: bytes, claimed_hash: str):
        if not isinstance(content, bytes) or not 0 < len(content) <= MAX_CHUNK_BYTES:
            raise DomainError('CHUNK_SIZE', '전송 조각의 크기가 올바르지 않습니다.', 413)
        measured = hashlib.sha256(content).hexdigest()
        if not isinstance(claimed_hash, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', claimed_hash) or measured != claimed_hash.lower():
            raise DomainError('CHUNK_HASH', '전송 중 자료가 바뀌었습니다. 해당 부분을 다시 보내세요.', 422)
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, WRITERS)
            batch = self._batch(c, company, batch_id)
            if batch['state'] != 'OPEN':
                raise DomainError('UPLOAD_NOT_OPEN', '종료되거나 중단된 전송입니다.')
            row = c.execute('SELECT manifest FROM upload_files WHERE company_id=? AND batch_id=? AND id=?', (company, batch_id, file_id)).fetchone()
            if not row:
                raise DomainError('NOT_FOUND', '전송할 파일이 없습니다.', 404)
            item = UploadFileInput.model_validate(json.loads(row[0]))
            try:
                expected_size = item.expected_chunk_bytes(index)
            except ValueError:
                raise DomainError('CHUNK_INDEX', '전송 조각 번호가 올바르지 않습니다.', 422)
            if len(content) != expected_size:
                raise DomainError('CHUNK_SIZE', '선언한 파일 크기와 전송 조각이 맞지 않습니다.', 422)
            prior = c.execute('SELECT sha256 FROM upload_chunks WHERE company_id=? AND batch_id=? AND file_id=? AND chunk_index=?', (company, batch_id, file_id, index)).fetchone()
            if prior:
                if prior[0] != measured:
                    raise DomainError('CHUNK_CONFLICT', '이미 받은 조각과 내용이 다릅니다.')
                return {'stored': True, 'reused': True, 'sha256': measured}
            c.execute('INSERT INTO upload_chunks VALUES(?,?,?,?,?,?)', (company, batch_id, file_id, index, measured, content))
            return {'stored': True, 'reused': False, 'sha256': measured}

    def complete(self, actor, company, ident, expected_revision):
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, WRITERS)
            batch = self._batch(c, company, ident)
            if batch['state'] == 'SEALED':
                return self._view(c, company, ident)
            self._revision(batch, expected_revision)
            if batch['state'] != 'OPEN':
                raise DomainError('UPLOAD_NOT_OPEN', '중단된 전송은 완료할 수 없습니다.')
            rows = c.execute('SELECT * FROM upload_files WHERE company_id=? AND batch_id=? ORDER BY id', (company, ident)).fetchall()
            for row in rows:
                item = UploadFileInput.model_validate(json.loads(row['manifest']))
                count, size = c.execute('SELECT count(*),COALESCE(sum(length(content)),0) FROM upload_chunks WHERE company_id=? AND batch_id=? AND file_id=?', (company, ident, row['id'])).fetchone()
                if count != item.chunk_count or size != item.size_bytes:
                    raise DomainError('UPLOAD_INCOMPLETE', '아직 받지 못한 파일 부분이 있습니다.', details={'file_id': row['id']})
            for row in rows:
                item = UploadFileInput.model_validate(json.loads(row['manifest']))
                raw = b''.join(r[0] for r in c.execute('SELECT content FROM upload_chunks WHERE company_id=? AND batch_id=? AND file_id=? ORDER BY chunk_index', (company, ident, row['id'])))
                if hashlib.sha256(raw).hexdigest() != item.sha256:
                    raise DomainError('FILE_HASH', '원본과 받은 파일이 일치하지 않습니다.', 422, {'file_id': row['id']})
                existing = c.execute('SELECT id FROM upload_documents WHERE company_id=? AND sha256=?', (company, item.sha256)).fetchone()
                if existing:
                    doc_id = existing[0]
                else:
                    scan = inspect_document(item.filename, raw)
                    ts, doc_id = now(), uid('document')
                    blob = c.execute('SELECT id FROM blobs WHERE company_id=? AND sha256=?', (company, item.sha256)).fetchone()
                    blob_id = blob[0] if blob else uid('evidence')
                    if not blob:
                        c.execute('INSERT INTO blobs VALUES(?,?,?,?,?,?,?,?,?)', (company, blob_id, item.sha256, item.filename, scan['mime'], raw, None, 'SOURCE_STORED_NOT_ACCOUNTED', ts))
                    initial_reader = scan['state'] == 'STRUCTURE_CHECKED' or (scan['state'] == 'READER_REQUIRED' and scan['code'] in ('TABLE_READER_REQUIRED', 'DOCUMENT_READER_REQUIRED'))
                    state = 'QUEUED' if initial_reader else scan['state']
                    c.execute('INSERT INTO upload_documents(company_id,id,sha256,blob_id,filename,size_bytes,mime,inspection,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                              (company, doc_id, item.sha256, blob_id, item.filename, item.size_bytes, scan['mime'], canonical(scan), state, ts, ts))
                    if state == 'QUEUED':
                        c.execute('INSERT INTO upload_jobs(id,company_id,document_id,requested_by,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                                  (uid('ingest'), company, doc_id, batch['submitted_by'], 'QUEUED', ts, ts))
                    self.db.audit(c, company, actor.id, 'upload.document_stored', doc_id, {'sha256': item.sha256, 'state': state, 'inspection': scan['code']})
                c.execute('UPDATE upload_files SET document_id=? WHERE company_id=? AND batch_id=? AND id=?', (doc_id, company, ident, row['id']))
            c.execute("UPDATE upload_batches SET state='SEALED',revision=revision+1,updated_at=? WHERE company_id=? AND id=?", (now(), company, ident))
            c.execute('DELETE FROM upload_chunks WHERE company_id=? AND batch_id=?', (company, ident))
            self.db.audit(c, company, actor.id, 'upload.sealed', ident, {'files': len(rows), 'accounting_complete': False})
            return self._view(c, company, ident)

    def cancel(self, actor, company, ident, expected_revision, reason):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise DomainError('REASON_REQUIRED', '중단 사유가 필요합니다.', 422)
        with self.db.transaction() as c:
            self.auth.guard(c, actor, company, WRITERS)
            batch = self._batch(c, company, ident)
            self._revision(batch, expected_revision)
            if batch['state'] != 'OPEN':
                raise DomainError('UPLOAD_NOT_OPEN', '이미 접수된 원본은 전송 취소로 삭제하지 않습니다.')
            c.execute("UPDATE upload_batches SET state='CANCELLED',reason=?,revision=revision+1,updated_at=? WHERE company_id=? AND id=?", (reason, now(), company, ident))
            c.execute('DELETE FROM upload_chunks WHERE company_id=? AND batch_id=?', (company, ident))
            self.db.audit(c, company, actor.id, 'upload.cancelled', ident, {'reason': reason})
            return self._view(c, company, ident)

    def document(self, actor, company, ident):
        with self.db.connect() as c:
            self.auth.guard(c, actor, company)
            row = c.execute('SELECT * FROM upload_documents WHERE company_id=? AND id=?', (company, ident)).fetchone()
            if not row:
                raise DomainError('NOT_FOUND', '자료를 찾을 수 없습니다.', 404)
            value = dict(row)
            value['inspection'] = json.loads(value['inspection'])
            value['result'] = json.loads(value['result']) if value['result'] else None
            value['accounting_complete'] = False
            return value
