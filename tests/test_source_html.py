"""Non-executing flat HTML table exports saved with an .xls extension."""
import pytest
from price.source_html import parse_source_html
from price.ingest_safety import inspect_document
from price.ingestion import Ingestion
from test_source_ingestion import upload

HTML='''<!doctype html><html><head><meta charset="utf-8"></head><body>
<p>계좌번호: 123-456-789012</p><p>통화: KRW</p>
<table><tr><th>거래일자</th><th>거래내용</th><th>입금액</th><th>출금액</th><th>잔액</th></tr>
<tr><td>2026-09-01</td><td>검증 입금</td><td>1,100</td><td>0</td><td>1,100</td></tr>
<tr><td>2026-09-02</td><td>검증 출금</td><td>0</td><td>220</td><td>880</td></tr>
<tr><td>합계</td><td></td><td>1100</td><td>220</td><td></td></tr></table></body></html>'''.encode()


def test_html_xls_reads_cells_without_executing_html():
    r=parse_source_html(HTML)
    assert r['state']=='NORMALIZED',r
    assert len(r['records'])==2 and r['records'][0]['values']['gross']=='1100'
    assert r['records'][0]['values']['account']=='123-456-789012'
    assert r['records'][0]['fields']['date']['source_format']=='html_table'
    assert r['records'][0]['fields']['date']['table']==1
    assert r['accounting_complete'] is False


def test_cp949_html_export_is_read_without_reencoding_by_owner():
    r=parse_source_html(HTML.decode().encode('cp949'))
    assert r['state']=='NORMALIZED' and r['encoding']=='cp949'


@pytest.mark.parametrize('fragment',[
    '<script>window.untrusted=1</script>', '<iframe src="https://example.invalid"></iframe>',
    '<img src="https://example.invalid/a">', '<a href="https://example.invalid">x</a>',
    '<div onclick="doSomething()">x</div>', '<style>@import "https://example.invalid";</style>',
    '<div style="background:url(https://example.invalid)">x</div>',
    '<meta http-equiv="refresh" content="0;url=https://example.invalid">',
    '<table><tr><td>nested</td></tr></table>',
])
def test_active_or_nested_content_never_reaches_normalized_records(fragment):
    raw=HTML.replace(b'<td>2026-09-01</td>',('<td>'+fragment+'</td>').encode())
    r=parse_source_html(raw)
    assert r['state']=='READ_FAILED' and not r['records'],r
    assert inspect_document('bank.xls',raw)['state']=='QUARANTINED'


def test_merged_data_cells_not_silently_expanded():
    raw=HTML.replace(b'<td>2026-09-01',b'<td colspan="2">2026-09-01')
    r=parse_source_html(raw)
    assert r['state']!='NORMALIZED'


def test_unknown_nonempty_second_table_is_not_omitted():
    raw=HTML.replace(b'</body>',b'<table><tr><td>Unknown transaction</td><td>999</td></tr></table></body>')
    r=parse_source_html(raw)
    assert r['state']=='VALIDATION_REQUIRED' and r['issues']


def test_empty_amount_cells_are_preserved():
    raw=HTML.replace(b'<td>0</td>',b'<td></td>')
    r=parse_source_html(raw)
    assert r['state']=='NORMALIZED' and len(r['records'])==2


def test_html_xls_real_upload_queue_selects_correct_reader(w):
    b=upload(w,HTML,filename='통장.xls');reader=Ingestion(w.svc)
    result=reader.run_once(w.company)
    assert result['state']=='SUCCEEDED' and result['records']==2,result
    assert reader.uploads.document(w.owner,w.company,b['files'][0]['document']['id'])['state']=='NORMALIZED'
