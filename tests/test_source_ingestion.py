from __future__ import annotations
import hashlib
from concurrent.futures import ThreadPoolExecutor
import pytest
from price.source_tables import parse_source_table, amount, date_value
from price.uploads import Uploads
from price.ingestion import Ingestion
from price.upload_models import UploadBatchInput
from price.db import DomainError

BANK = ('합성 검증용 거래내역\n계좌번호: 123-456-789012\n통화: KRW\n'
        '거래일시,거래내용,입금액,출금액,거래후잔액\n'
        '2026-09-01 10:00:00,용역대금,"1,100",0,"1,100"\n'
        '2026-09-02 10:00:00,장비지급,0,220,880\n'
        '합계,,1100,220,\n').encode()
INVOICES = ('작성일자,승인번호,공급자사업자등록번호,공급받는자사업자등록번호,공급가액(원),세액,합계금액\n'
            '2026-09-01,SYNTHETIC-01,111-11-11111,222-22-22222,1000,100,1100\n').encode()
CARD = ('승인일자,카드번호,가맹점명,승인금액,승인번호,승인취소구분,통화\n'
        '2026-09-01,1111-****-****-1111,시험상점,1100,SYNTHETIC-01,승인,KRW\n'
        '2026-09-02,1111-****-****-1111,시험상점,-1100,SYNTHETIC-02,취소,KRW\n').encode()


def upload(w, raw=BANK, filename='통장.csv', key='source-test-01'):
    u = Uploads(w.svc)
    data = UploadBatchInput(idempotency_key=key, files=[{'client_file_id': 'source1', 'filename': filename, 'size_bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}])
    b = u.begin(w.owner, w.company, data)
    u.put_chunk(w.owner, w.company, b['id'], 'source1', 0, raw, hashlib.sha256(raw).hexdigest())
    return u.complete(w.owner, w.company, b['id'], b['revision'])


@pytest.mark.parametrize('raw,kind,count', [(BANK, 'bank_statement', 2), (INVOICES, 'tax_invoice_list', 1), (CARD, 'card_transactions', 2)], ids=['bank', 'invoice-list', 'card'])
def test_supported_header_facts_have_exact_provenance(raw, kind, count):
    r = parse_source_table(raw)
    assert r['state'] == 'NORMALIZED', r
    assert r['document_kind'] == kind and len(r['records']) == count
    assert r['accounting_complete'] is False and r['institution_verified'] is False
    assert all(x['fields']['date']['row'] == x['row'] for x in r['records'])


def test_bank_direction_and_source_totals():
    r = parse_source_table(BANK)
    first, second = r['records']
    assert first['values']['gross'] == '1100'
    assert first['values']['direction'] == 'receipt'
    assert second['values']['direction'] == 'payment'
    assert second['values']['balance'] == '880'
    assert first['values']['account'] == '123-456-789012'
    assert r['controls'][0]['checks']['credit']['difference'] == '0'
    assert first['values']['tax'] is None


def test_no_vat_invention_from_card_or_bank():
    for raw in (BANK, CARD):
        for record in parse_source_table(raw)['records']:
            assert record['values']['tax'] is None
            assert record['values']['tax_inferred'] is False
    assert parse_source_table(CARD)['records'][1]['values']['gross'] == '-1100'


def test_invoice_does_not_choose_company_or_deductibility():
    r = parse_source_table(INVOICES)['records'][0]
    assert r['values']['direction'] is None
    assert 'deductible' not in r['values']
    assert 'company_ownership' in r['context_pending']


@pytest.mark.parametrize('value,expected', [('1,234', '1234'), ('(1,234)', '-1234'), ('₩ 100', '100'), ('100원', '100'), ('12.34', '12.34'), ('-2', '-2'), ('', None)])
def test_decimal_exact_source_amount(value, expected):
    assert amount(value) == expected


@pytest.mark.parametrize('value', ['1,23', '1e6', 'NaN', '1/2', '100.3.4', '=SUM(A1)', '++20'])
def test_ambiguous_amounts_not_coerced(value):
    with pytest.raises(ValueError):
        amount(value)


@pytest.mark.parametrize('value', ['2026-02-30', '09/01/26', 'yesterday'])
def test_dates_not_guessed(value):
    with pytest.raises(ValueError):
        date_value(value)


def test_partial_rows_are_never_silently_omitted():
    raw = INVOICES + b'2026-09-01,SYNTHETIC-02,111,222,xyz,10,110\n'
    r = parse_source_table(raw)
    assert r['state'] == 'VALIDATION_REQUIRED'
    assert r['source_rows'] == 2 and len(r['records']) == 1 and r['issues']


def test_total_mismatch_blocks_normalization():
    r = parse_source_table(BANK.replace(b'1100,220,\n', b'1200,220,\n'))
    assert r['state'] == 'VALIDATION_REQUIRED'
    assert any(x['code'] == 'CONTROL_TOTAL_MISMATCH' for x in r['issues'])


def test_invoice_total_not_balanced_by_fabrication():
    r = parse_source_table(INVOICES.replace(b'1000,100,1100', b'1000,100,1200'))
    assert r['state'] == 'VALIDATION_REQUIRED'
    assert not r['records']


def test_cp949_tsv_no_user_column_mapping():
    text = CARD.decode().replace(',', '\t')
    r = parse_source_table(text.encode('cp949'))
    assert r['encoding'] == 'cp949' and r['state'] == 'NORMALIZED'


def test_unknown_and_repeated_header_handling():
    assert parse_source_table(b'unknown,a,b\n1,2,3\n')['state'] == 'FORMAT_REVIEW'
    header = INVOICES.decode().splitlines()[0]
    r = parse_source_table(INVOICES + (header + '\n').encode())
    assert r['record_count'] == 1 and r['omitted_rows'][0]['reason'] == 'repeated_header'


def test_formula_cell_held_without_execution():
    r = parse_source_table(CARD.replace('시험상점'.encode(), b'=SYNTHETIC_FORMULA'))
    assert r['state'] == 'VALIDATION_REQUIRED'
    assert all(x['code'] == 'CELL_FORMULA_REVIEW' for x in r['issues'] if x['row'])


def test_upload_alone_yields_durable_source_cases(w):
    batch = upload(w)
    doc = batch['files'][0]['document']['id']
    processor = Ingestion(w.svc)
    result = processor.run_once(w.company)
    assert result['state'] == 'SUCCEEDED' and result['cases'] == 2
    assert not processor.run_once(w.company)['processed']
    records = processor.records(w.owner, w.company, doc)
    assert records['total'] == 2
    with w.db.connect() as c:
        assert len(w.db.list(c, w.company, 'ingestion_case')) == 2
        assert not w.db.list(c, w.company, 'journal')
        assert w.db.verify_audit(c, w.company)['valid']
    batch = processor.uploads.get(w.owner, w.company, batch['id'])
    assert batch['processing_status'] == 'READ_COMPLETE_ACCOUNTING_PENDING'
    assert batch['accounting_complete'] is False


def test_duplicate_source_reupload_never_reprocesses(w):
    first = upload(w)
    reader = Ingestion(w.svc); reader.run_once(w.company)
    second = upload(w, key='source-test-02', filename='사본.csv')
    assert first['files'][0]['document']['id'] == second['files'][0]['document']['id']
    assert not reader.run_once(w.company)['processed']


def test_partial_file_keeps_candidates_without_business_cases(w):
    b = upload(w, INVOICES + b'bad,row\n')
    r = Ingestion(w.svc).run_once(w.company)
    assert r['state'] == 'BLOCKED'
    with w.db.connect() as c:
        assert len(w.db.list(c, w.company, 'source_record')) == 1
        assert not w.db.list(c, w.company, 'ingestion_case')


def test_lease_fencing_and_double_finish(w):
    upload(w)
    reader = Ingestion(w.svc)
    stale = reader.claim(w.company)
    with w.db.transaction() as c:
        c.execute('UPDATE upload_jobs SET lease_until=0 WHERE id=?', (stale['id'],))
    current = reader.claim(w.company)
    assert current['lease_token'] != stale['lease_token']
    assert reader.finish(stale, parse_source_table(BANK))['committed'] is False
    assert reader.finish(current, parse_source_table(BANK))['committed'] is True
    assert reader.finish(current, parse_source_table(BANK))['committed'] is False
    with w.db.connect() as c:
        assert len(w.db.list(c, w.company, 'source_record')) == 2


def test_revoked_uploader_cannot_publish_pending_data(w):
    upload(w)
    reader = Ingestion(w.svc); job = reader.claim(w.company)
    with w.db.transaction() as c:
        c.execute('DELETE FROM memberships WHERE company_id=? AND user_id=?', (w.company, w.owner.id))
    r = reader.finish(job, parse_source_table(BANK))
    assert r['error'] == 'UPLOAD_AUTHORIZATION_REVOKED'
    with w.db.connect() as c:
        assert not w.db.list(c, w.company, 'source_record')


def test_other_company_source_records_inaccessible(w):
    b = upload(w); processor = Ingestion(w.svc); processor.run_once(w.company)
    with pytest.raises(DomainError):
        processor.records(w.other_owner, w.company, b['files'][0]['document']['id'])
