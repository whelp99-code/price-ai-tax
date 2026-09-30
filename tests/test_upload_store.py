from __future__ import annotations
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from price.db import DomainError, Database
from price.service import Service
from price.uploads import Uploads
from price.upload_models import UploadBatchInput, MAX_CHUNK_BYTES

RAW = '거래일자,거래내용,입금액,출금액,거래후잔액\n2026-09-01,입금,100,0,100\n'.encode()


def plan(raw=RAW, key='request-0001', filename='은행.csv', ident='file1'):
    return UploadBatchInput(idempotency_key=key, files=[{'client_file_id': ident, 'filename': filename, 'size_bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}])


def push(u, w, batch, raw=RAW, file_id='file1'):
    for i, offset in enumerate(range(0, len(raw), MAX_CHUNK_BYTES)):
        part = raw[offset:offset + MAX_CHUNK_BYTES]
        u.put_chunk(w.owner, w.company, batch['id'], file_id, i, part, hashlib.sha256(part).hexdigest())


def seal(u, w, raw=RAW, **options):
    b = u.begin(w.owner, w.company, plan(raw, **options))
    push(u, w, b, raw)
    return u.complete(w.owner, w.company, b['id'], b['revision'])


def test_begin_idempotent_manifest_and_conflict(w):
    u = Uploads(w.svc)
    a = u.begin(w.owner, w.company, plan())
    b = u.begin(w.owner, w.company, plan())
    assert a['id'] == b['id'] and b['reused']
    with pytest.raises(DomainError, match='같은 전송 번호'):
        u.begin(w.owner, w.company, plan(RAW + b'\n'))


def test_resume_and_whole_digest(w):
    u = Uploads(w.svc)
    raw = b'x' * (MAX_CHUNK_BYTES + 17)
    b = u.begin(w.owner, w.company, plan(raw))
    part = raw[:MAX_CHUNK_BYTES]
    u.put_chunk(w.owner, w.company, b['id'], 'file1', 0, part, hashlib.sha256(part).hexdigest())
    second = Uploads(Service(Database(w.db.path)))
    view = second.get(w.owner, w.company, b['id'])
    assert view['files'][0]['received_bytes'] == MAX_CHUNK_BYTES
    assert second.put_chunk(w.owner, w.company, b['id'], 'file1', 0, part, hashlib.sha256(part).hexdigest())['reused']
    tail = raw[MAX_CHUNK_BYTES:]
    second.put_chunk(w.owner, w.company, b['id'], 'file1', 1, tail, hashlib.sha256(tail).hexdigest())
    done = second.complete(w.owner, w.company, b['id'], b['revision'])
    doc = second.document(w.owner, w.company, done['files'][0]['document']['id'])
    content = w.svc.evidence_content(w.owner, w.company, doc['blob_id'])
    assert content['content'] == raw
    assert not done['accounting_complete']


def test_incomplete_batch_does_not_create_original_or_job(w):
    u = Uploads(w.svc)
    b = u.begin(w.owner, w.company, plan())
    with pytest.raises(DomainError) as error:
        u.complete(w.owner, w.company, b['id'], b['revision'])
    assert error.value.code == 'UPLOAD_INCOMPLETE'
    with w.db.connect() as c:
        assert c.execute('SELECT count(*) FROM upload_documents').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM upload_jobs').fetchone()[0] == 0


def test_wrong_whole_hash_is_atomic(w):
    u = Uploads(w.svc)
    p = plan().model_dump()
    p['files'][0]['sha256'] = '0' * 64
    b = u.begin(w.owner, w.company, UploadBatchInput(**p))
    push(u, w, b)
    with pytest.raises(DomainError) as error:
        u.complete(w.owner, w.company, b['id'], b['revision'])
    assert error.value.code == 'FILE_HASH'
    with w.db.connect() as c:
        assert c.execute('SELECT count(*) FROM upload_documents').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM upload_chunks').fetchone()[0] == 1


@pytest.mark.parametrize('index,data,claimed,code', [
    (0, RAW, '0' * 64, 'CHUNK_HASH'),
    (-1, RAW, hashlib.sha256(RAW).hexdigest(), 'CHUNK_INDEX'),
    (True, RAW, hashlib.sha256(RAW).hexdigest(), 'CHUNK_INDEX'),
    (1, RAW, hashlib.sha256(RAW).hexdigest(), 'CHUNK_INDEX'),
    (0, b'', hashlib.sha256(b'').hexdigest(), 'CHUNK_SIZE'),
    (0, b'x', hashlib.sha256(b'x').hexdigest(), 'CHUNK_SIZE'),
])
def test_bad_chunk_has_no_effect(w, index, data, claimed, code):
    u = Uploads(w.svc); b = u.begin(w.owner, w.company, plan())
    with pytest.raises(DomainError) as error:
        u.put_chunk(w.owner, w.company, b['id'], 'file1', index, data, claimed)
    assert error.value.code == code
    assert u.get(w.owner, w.company, b['id'])['files'][0]['received_bytes'] == 0


def test_chunk_replacement_rejected(w):
    u = Uploads(w.svc); b = u.begin(w.owner, w.company, plan())
    push(u, w, b)
    raw = b'x' * len(RAW)
    with pytest.raises(DomainError) as error:
        u.put_chunk(w.owner, w.company, b['id'], 'file1', 0, raw, hashlib.sha256(raw).hexdigest())
    assert error.value.code == 'CHUNK_CONFLICT'


def test_duplicate_filename_change_no_duplicate_source_or_job(w):
    u = Uploads(w.svc)
    first = seal(u, w)
    again = seal(u, w, key='request-0002', filename='다른이름.csv')
    assert first['files'][0]['document']['id'] == again['files'][0]['document']['id']
    with w.db.connect() as c:
        assert c.execute('SELECT count(*) FROM upload_jobs').fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM upload_documents').fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM upload_chunks').fetchone()[0] == 0
    assert u.complete(w.owner, w.company, first['id'], 1)['state'] == 'SEALED'


def test_company_role_and_revocation(w):
    u = Uploads(w.svc); b = u.begin(w.owner, w.company, plan())
    for actor in (w.viewer, w.other_owner):
        with pytest.raises(DomainError):
            u.put_chunk(actor, w.company, b['id'], 'file1', 0, RAW, hashlib.sha256(RAW).hexdigest())
    with pytest.raises(DomainError):
        u.get(w.other_owner, w.company, b['id'])
    with pytest.raises(DomainError):
        u.begin(w.viewer, w.company, plan(key='request-viewer'))
    with w.db.transaction() as c:
        c.execute('DELETE FROM memberships WHERE user_id=? AND company_id=?', (w.owner.id, w.company))
    with pytest.raises(DomainError):
        push(u, w, b)


def test_cancel_only_transport_fragments(w):
    u = Uploads(w.svc); b = u.begin(w.owner, w.company, plan())
    push(u, w, b)
    cancelled = u.cancel(w.owner, w.company, b['id'], b['revision'], '선택한 파일 변경')
    assert cancelled['state'] == 'CANCELLED'
    with pytest.raises(DomainError):
        u.complete(w.owner, w.company, b['id'], cancelled['revision'])
    with pytest.raises(DomainError):
        push(u, w, b)
    assert w.svc.evidence_content(w.owner, w.company, w.evidence)['content']


def test_quarantine_does_not_enqueue(w):
    u = Uploads(w.svc)
    done = seal(u, w, b'not a PDF', filename='잘못된.pdf')
    assert done['processing_status'] == 'NEEDS_ATTENTION'
    assert done['files'][0]['document']['state'] == 'QUARANTINED'
    with w.db.connect() as c:
        assert c.execute('SELECT count(*) FROM upload_jobs').fetchone()[0] == 0


def test_concurrent_chunk_idempotence(w):
    u = Uploads(w.svc); b = u.begin(w.owner, w.company, plan())
    def action(_):
        return u.put_chunk(w.owner, w.company, b['id'], 'file1', 0, RAW, hashlib.sha256(RAW).hexdigest())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(action, range(2)))
    assert sum(not r['reused'] for r in results) == 1
    assert len(u.get(w.owner, w.company, b['id'])['files'][0]['received_chunks']) == 1


def test_stale_complete_and_no_deleting_sealed(w):
    u = Uploads(w.svc); b = u.begin(w.owner, w.company, plan()); push(u, w, b)
    with pytest.raises(DomainError):
        u.complete(w.owner, w.company, b['id'], 99)
    done = u.complete(w.owner, w.company, b['id'], 1)
    with pytest.raises(DomainError):
        u.cancel(w.owner, w.company, b['id'], done['revision'], '이미 보관된 원본')


def test_upload_migration_repeat_preserves_original_ledger(w):
    posted = w.post(w.tx())
    Uploads(w.svc); Uploads(w.svc)
    assert w.svc.get_object(w.owner, w.company, 'journal', posted['id']) == posted
    with w.db.connect() as c:
        assert w.db.verify_audit(c, w.company)['valid']
        assert c.execute('SELECT max(version) FROM schema_version').fetchone()[0] == 1


def test_open_batch_quota(w):
    u = Uploads(w.svc)
    for i in range(20):
        u.begin(w.owner, w.company, plan(key=f'request-quota-{i}'))
    with pytest.raises(DomainError) as error:
        u.begin(w.owner, w.company, plan(key='request-quota-final'))
    assert error.value.code == 'UPLOAD_QUOTA'
