from price.source_periods import printed_query_period,is_no_activity_line
from price.source_tables import parse_source_table
from price.ingestion import Ingestion
from price.casework import Casework
from price.finance_ops import uploaded_source_blockers
from test_source_ingestion import upload,BANK

HEADER='계좌번호: 123-456-789012\n통화: KRW\n조회기간: 2026-09-01 ~ 2026-09-30\n거래일자,거래내용,입금액,출금액,잔액\n'


def test_printed_period_not_inferred_from_transaction_dates():
    no_range=parse_source_table(BANK)
    assert not no_range['metadata'].get('declared_period')
    declared=printed_query_period('조회기간: 2026.9.1 ~ 2026.9.30')
    assert declared['declared_period']['start']=='2026-09-01'
    assert declared['declared_period']['end']=='2026-09-30'
    assert declared['declared_period']['basis']=='printed_query_period_not_provider_verified'


def test_invalid_or_conflicting_query_period_is_not_chosen_silently():
    assert printed_query_period('조회기간: 2026-09-30 ~ 2026-09-01')['declared_period_invalid']
    assert printed_query_period('조회기간: 2026-02-30 ~ 2026-03-31')['declared_period_invalid']
    assert printed_query_period('조회기간: 2026-09-01 ~ 2026-09-30\n조회기간: 2026-08-01 ~ 2026-08-31')['declared_period_conflict']


def test_explicit_zero_rows_are_distinct_from_unreadable_or_empty():
    r=parse_source_table((HEADER+'조회된 거래내역이 없습니다.\n').encode())
    assert r['state']=='NO_ACTIVITY_REPORTED' and r['record_count']==0
    assert r['no_activity_declared'] and not r['coverage_verified'] and not r['accounting_complete']
    empty=parse_source_table(HEADER.encode())
    assert empty['state']=='VALIDATION_REQUIRED' and empty['issues'][0]['code']=='NO_VALID_RECORDS'


def test_zero_activity_without_explicit_query_period_stays_unverified():
    r=parse_source_table((HEADER.replace('조회기간: 2026-09-01 ~ 2026-09-30\n','')+'거래내역이 없습니다\n').encode())
    assert r['state']=='VALIDATION_REQUIRED'
    assert not r.get('no_activity_declared')


def test_zero_claim_and_transaction_conflict_cannot_be_normalized():
    r=parse_source_table((HEADER+'거래내역이 없습니다\n2026-09-02,거래,100,0,100\n').encode())
    assert r['state']=='VALIDATION_REQUIRED'
    assert any(i['code']=='NO_ACTIVITY_CONFLICT' for i in r['issues'])


def test_zero_marker_with_nonempty_amount_not_skipped():
    assert is_no_activity_line(['거래내역없음','',''])
    assert not is_no_activity_line(['거래내역없음','5000'])
    r=parse_source_table((HEADER+'거래내역없음,5000\n').encode())
    assert r['state']=='VALIDATION_REQUIRED'


def test_zero_activity_pipeline_stays_pending_independent_coverage(w):
    batch=upload(w,(HEADER+'거래내역이 없습니다\n').encode())
    reader=Ingestion(w.svc);result=reader.run_once(w.company)
    document=reader.uploads.document(w.owner,w.company,batch['files'][0]['document']['id'])
    assert result['state']=='BLOCKED' and document['state']=='NO_ACTIVITY_REPORTED'
    assert not result['records'] and not result['cases']
    assert not document['accounting_complete']
    Casework(w.svc).refresh(w.owner,w.company)
    with w.db.connect() as c:
        assert not w.db.list(c,w.company,'economic_event')
        blockers=uploaded_source_blockers(w.db,c,w.company,{'start':'2026-09-01','end':'2026-09-30'})
        assert any(b['code']=='UNRESOLVED_SOURCE_DOCUMENT' for b in blockers)


def test_blank_or_no_activity_lines_cannot_bypass_resource_limits():
    for body in ('\n'*20200, '거래내역없음\n'*20200):
        result=parse_source_table((HEADER+body).encode())
        assert result['state']=='VALIDATION_REQUIRED'
        assert any(i['code']=='TABLE_RESOURCE_LIMIT' for i in result['issues'])


def test_explicit_zero_activity_survives_xlsx_and_html_containers():
    from test_source_workbooks import make_workbook
    from price.source_workbooks import parse_source_workbook
    from price.source_html import parse_source_html
    rows=[['계좌번호: 123456789012'],['통화: KRW'],['조회기간: 2026-09-01 ~ 2026-09-30'],['거래일자','거래내용','입금액','출금액','잔액'],['조회된 거래내역이 없습니다']]
    workbook=parse_source_workbook(make_workbook([('조회내역',rows),('빈시트',[])]))
    html=('<html><body><p>계좌번호: 123456789012</p><p>조회기간: 2026-09-01 ~ 2026-09-30</p><table><tr><th>거래일자</th><th>거래내용</th><th>입금액</th><th>출금액</th><th>잔액</th></tr><tr><td>거래내역이 없습니다</td></tr></table></body></html>').encode()
    for result in (workbook,parse_source_html(html)):
        assert result['state']=='NO_ACTIVITY_REPORTED' and result['no_activity_declared']
        assert not result['coverage_verified'] and not result['accounting_complete']
        assert result['metadata']['declared_period']['end']=='2026-09-30'
