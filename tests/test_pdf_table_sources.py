from __future__ import annotations
from price.source_pdf_tables import parse_pdf_table_text

TEXT=('계좌번호: 123-456-789012\n통화: KRW\n'
      '거래일시    거래내용    입금액    출금액    거래후잔액\n'
      '2026-09-01 10:00:00    용역 대금    1,100    0    1,100\n'
      '2026-09-02 10:00:00    장비 지급    0    220    880\n'
      '합계    -    1100    220    -\n')


def test_pdf_layout_table_preserves_page_line_and_quoted_amounts():
    r=parse_pdf_table_text(TEXT)
    assert r['state']=='NORMALIZED',r
    assert r['records'][0]['values']['gross']=='1100'
    assert r['records'][0]['values']['description']=='용역 대금'
    assert r['records'][0]['fields']['date']['page']==1
    assert r['records'][0]['fields']['date']['source_line']==4
    assert r['ocr_used'] is False and r['statutory_validated'] is False


def test_two_pdf_pages_keep_same_headers_without_row_duplication():
    lines=TEXT.splitlines()
    text='\n'.join(lines[:4])+'\n\f'+lines[2]+'\n'+'\n'.join(lines[4:])
    r=parse_pdf_table_text(text)
    assert r['state']=='NORMALIZED' and len(r['records'])==2
    assert r['records'][1]['fields']['date']['page']==2
    assert r['records'][1]['fields']['date']['source_line']==2


def test_ambiguous_missing_pdf_cells_are_not_guessed():
    r=parse_pdf_table_text(TEXT.replace('0    220    880','220    880'))
    assert r['state']=='VALIDATION_REQUIRED'
    assert any(x['code']=='COLUMN_COUNT_MISMATCH' for x in r['issues'])


def test_unknown_layout_is_not_arbitrary_ai_extraction():
    r=parse_pdf_table_text('사진만 포함된 페이지 또는 알 수 없는 표')
    assert r['state']=='FORMAT_REVIEW' and r['records']==[]


def test_extra_unreadable_pdf_row_remains_visible():
    r=parse_pdf_table_text(TEXT+'읽기 어려운 거래가 더 있습니다\n')
    assert r['state']=='VALIDATION_REQUIRED' and r['source_rows']==3


def test_pdf_invoice_header_with_units_keeps_amounts_and_provenance():
    text=('작성일자    승인번호    공급자사업자등록번호    공급받는자사업자등록번호    공급가액(원)    세액    합계금액\n'
          '2026-09-01    SYNTHETIC-01    1111111111    2222222222    1000    100    1100\n')
    r=parse_pdf_table_text(text)
    assert r['state']=='NORMALIZED' and r['records'][0]['values']['currency']=='KRW'
    assert r['records'][0]['values']['direction'] is None
