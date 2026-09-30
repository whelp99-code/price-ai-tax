from __future__ import annotations
import io
import zipfile
from xml.etree.ElementTree import Element, SubElement, tostring
import pytest
from price.source_workbooks import parse_source_workbook, excel_date, cell_position, NS


# Synthetic OOXML fixtures, not claimed to be exports obtained from a bank.
def make_workbook(sheets, *, date1904=False, shared=False):
    out = io.BytesIO(); namespace = NS['s']
    def tag(name): return '{'+namespace+'}'+name
    book = Element(tag('workbook'))
    SubElement(book,tag('workbookPr'),{'date1904':'1' if date1904 else '0'})
    listing = SubElement(book,tag('sheets'))
    relationships = Element('Relationships',{'xmlns':'http://schemas.openxmlformats.org/package/2006/relationships'})
    strings = []
    parts = {}
    for n,(name,rows) in enumerate(sheets,1):
        SubElement(listing,tag('sheet'),{'name':name,'sheetId':str(n),'{'+NS['r']+'}id':'rId'+str(n)})
        SubElement(relationships,'Relationship',{'Id':'rId'+str(n),'Type':NS['r']+'/worksheet','Target':'worksheets/sheet'+str(n)+'.xml'})
        sheet = Element(tag('worksheet')); data = SubElement(sheet,tag('sheetData'))
        for r,values in enumerate(rows,1):
            row = SubElement(data,tag('row'),{'r':str(r)})
            for c,value in enumerate(values,1):
                if value is None: continue
                letter='';number=c
                while number: number,rem=divmod(number-1,26);letter=chr(65+rem)+letter
                attrs={'r':letter+str(r)}
                if isinstance(value,tuple):
                    typ,raw=value
                    cell=SubElement(row,tag('c'),{**attrs,'s':'1'} if typ=='date' else attrs)
                    if typ=='formula': SubElement(cell,tag('f')).text='1000+100'
                    SubElement(cell,tag('v')).text=str(raw)
                elif type(value) is int:
                    cell=SubElement(row,tag('c'),attrs);SubElement(cell,tag('v')).text=str(value)
                elif shared:
                    attrs['t']='s';cell=SubElement(row,tag('c'),attrs);SubElement(cell,tag('v')).text=str(len(strings));strings.append(value)
                else:
                    attrs['t']='inlineStr';cell=SubElement(row,tag('c'),attrs);inline=SubElement(cell,tag('is'));SubElement(inline,tag('t')).text=str(value)
        parts['xl/worksheets/sheet'+str(n)+'.xml']=tostring(sheet,encoding='utf-8')
    styles=Element(tag('styleSheet'));xfs=SubElement(styles,tag('cellXfs'),{'count':'2'});SubElement(xfs,tag('xf'),{'numFmtId':'0'});SubElement(xfs,tag('xf'),{'numFmtId':'14'})
    parts.update({'[Content_Types].xml':b'<Types/>','xl/workbook.xml':tostring(book,encoding='utf-8'),'xl/_rels/workbook.xml.rels':tostring(relationships,encoding='utf-8'),'xl/styles.xml':tostring(styles,encoding='utf-8')})
    if shared:
        table=Element(tag('sst'))
        for value in strings: SubElement(SubElement(table,tag('si')),tag('t')).text=value
        parts['xl/sharedStrings.xml']=tostring(table,encoding='utf-8')
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        for name,content in parts.items(): z.writestr(name,content)
    return out.getvalue()


BANK_ROWS=[['합성 통장내역'],['계좌번호: 123-456-789012'],['통화: KRW'],
           ['거래일자','거래내용','입금액','출금액','거래후잔액'],
           ['2026-09-01','용역대금',1100,0,1100],['2026-09-02','지급',0,220,880],
           ['합계',None,1100,220]]


@pytest.mark.parametrize('shared',[False,True])
def test_stored_ooxml_values_source_cells_and_totals(shared):
    result=parse_source_workbook(make_workbook([('거래내역',BANK_ROWS)],shared=shared))
    assert result['state']=='NORMALIZED', result
    assert result['record_count']==2 and result['source_rows']==2
    assert result['records'][0]['fields']['credit']['cell']=='C5'
    assert result['records'][0]['fields']['credit']['raw_stored_value']=='1100'
    assert result['records'][0]['sheet']=='거래내역'
    assert result['records'][0]['values']['account']=='123-456-789012'
    assert result['sheets'][0]['controls'][0]['checks']['credit']['difference']=='0'
    assert result['institution_verified'] is False


@pytest.mark.parametrize('serial,epoch,expected', [('40729',False,'2011-07-05'),('39267',True,'2011-07-05'),('1',False,'1900-01-01'),('61',False,'1900-03-01'),('40729.5',False,'2011-07-05 12:00:00')])
def test_excel_date_systems_not_interchanged(serial,epoch,expected):
    assert excel_date(serial,epoch)==expected


@pytest.mark.parametrize('serial',['60','-1','NaN','1e12'])
def test_impossible_excel_dates_rejected(serial):
    with pytest.raises(ValueError): excel_date(serial)


def test_numeric_date_cell_requires_style():
    rows=[['거래일자','거래내용','입금액','출금액','거래후잔액'],[('date','40729'),'수금',100,0,100]]
    result=parse_source_workbook(make_workbook([('내역',rows)]))
    assert result['records'][0]['values']['date']=='2011-07-05'
    assert result['records'][0]['fields']['date']['raw_stored_value']=='40729'
    rows[1][0]=40729
    assert parse_source_workbook(make_workbook([('내역',rows)]))['state']=='VALIDATION_REQUIRED'


def test_formula_cached_value_not_trusted():
    rows=[['거래일자','거래내용','입금액','출금액','거래후잔액'],['2026-09-01','수금',('formula',1100),0,1100]]
    result=parse_source_workbook(make_workbook([('내역',rows)]))
    assert result['state']=='VALIDATION_REQUIRED'
    assert any(i['code']=='FORMULA_CELL_REVIEW' for i in result['issues'])


def test_multiple_sheets_keep_equal_row_numbers_distinct():
    result=parse_source_workbook(make_workbook([('계좌A',BANK_ROWS),('계좌B',BANK_ROWS)]))
    assert result['record_count']==4
    assert {r['sheet'] for r in result['records']}=={'계좌A','계좌B'}
    assert result['state']=='NORMALIZED'


def test_unknown_nonempty_sheet_not_silently_omitted():
    result=parse_source_workbook(make_workbook([('내역',BANK_ROWS),('미확인자료',[['unknown'],['not discarded']])]))
    assert result['state']=='VALIDATION_REQUIRED'
    assert any(i['code']=='TABLE_HEADER_UNKNOWN' for i in result['issues'])


@pytest.mark.parametrize('reference',['XFD1048576','A0','A-1','../A1','a1','ZZZ1'])
def test_bad_or_excessive_cell_positions(reference):
    with pytest.raises(ValueError): cell_position(reference)


def test_missing_shared_string_rejected_not_numeric_id():
    raw=make_workbook([('내역',BANK_ROWS)],shared=True)
    source=zipfile.ZipFile(io.BytesIO(raw));out=io.BytesIO()
    with source,zipfile.ZipFile(out,'w') as target:
        for name in source.namelist():
            if name!='xl/sharedStrings.xml': target.writestr(name,source.read(name))
    result=parse_source_workbook(out.getvalue())
    assert result['state']=='VALIDATION_REQUIRED'
    assert any(i['code']=='SHARED_STRING_MISSING' for i in result['issues'])
