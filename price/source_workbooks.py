"""Bounded OOXML table reader. Reads stored values; never evaluates formulas.

This module supports a deliberately narrow, tested source-workbook subset.
Unknown worksheets, formula cells, unsupported dates and missing shared strings
are review states rather than guessed accounting data.
"""
from __future__ import annotations
import csv
import io
import posixpath
import re
import zipfile
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from .ingest_safety import inspect_xlsx, safe_xml
from .source_tables import parse_source_table, MAX_ROWS, MAX_COLUMNS, MAX_CELLS

NS = {'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
      'r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
VERSION = 'bounded-source-xlsx/2'


def cell_position(reference):
    match = re.fullmatch(r'([A-Z]{1,3})([1-9][0-9]{0,6})', reference or '')
    if not match:
        raise ValueError('CELL_REFERENCE_INVALID')
    col = 0
    for letter in match[1]:
        col = col * 26 + ord(letter) - 64
    row = int(match[2])
    if col > MAX_COLUMNS or row > MAX_ROWS + 60:
        raise ValueError('WORKSHEET_RESOURCE_LIMIT')
    return row, col


def excel_date(value, epoch1904=False):
    serial = Decimal(value)
    if not serial.is_finite() or serial < 0 or serial > 2_000_000:
        raise ValueError('EXCEL_DATE_INVALID')
    whole = int(serial.to_integral_value(rounding=ROUND_FLOOR))
    if not epoch1904 and whole == 60:
        raise ValueError('EXCEL_FICTITIOUS_LEAP_DATE')
    seconds = (serial - whole) * 86400
    # Precision is retained in the raw value; ambiguous subsecond dates stay under review.
    nearest = seconds.to_integral_value()
    if abs(seconds - nearest) > Decimal('0.0001'):
        raise ValueError('EXCEL_TIME_PRECISION_REVIEW')
    origin = datetime(1904,1,1) if epoch1904 else datetime(1899,12,31)
    days = whole if epoch1904 or whole < 60 else whole - 1
    result = origin + timedelta(days=days, seconds=int(nearest))
    return result.strftime('%Y-%m-%d %H:%M:%S') if nearest else result.strftime('%Y-%m-%d')


def _styles(archive):
    if 'xl/styles.xml' not in archive.namelist():
        return set()
    root = safe_xml(archive.read('xl/styles.xml'))
    custom = {int(n.attrib['numFmtId']): n.attrib.get('formatCode','') for n in root.findall('s:numFmts/s:numFmt',NS)}
    dates = set()
    for i, xf in enumerate(root.findall('s:cellXfs/s:xf',NS)):
        fmt_id = int(xf.attrib.get('numFmtId','0'))
        code = custom.get(fmt_id,'')
        stripped = re.sub(r'"[^"]*"|\\.|\[[^]]*\]', '', code).lower()
        if fmt_id in (14,15,16,17,18,19,20,21,22,45,46,47) or ('y' in stripped and 'd' in stripped):
            dates.add(i)
    return dates


def _strings(archive):
    if 'xl/sharedStrings.xml' not in archive.namelist():
        return []
    root = safe_xml(archive.read('xl/sharedStrings.xml'))
    strings = []
    for item in root.findall('s:si',NS):
        parts = []
        for child in item:
            tag = child.tag.rsplit('}',1)[-1]
            if tag == 't':
                parts.append(child.text or '')
            elif tag == 'r':
                parts.extend(n.text or '' for n in child.findall('s:t',NS))
        text = ''.join(parts)
        if len(text) > 65536 or len(strings) >= MAX_CELLS:
            raise ValueError('SHARED_STRINGS_RESOURCE_LIMIT')
        strings.append(text)
    return strings


def _cell(cell, strings, dates, epoch1904):
    typ = cell.attrib.get('t','n')
    raw = cell.findtext('s:v', default='', namespaces=NS)
    if cell.find('s:f',NS) is not None:
        raise ValueError('FORMULA_CELL_REVIEW')
    if typ == 'inlineStr':
        item = cell.find('s:is',NS)
        if item is None:
            raise ValueError('INLINE_STRING_INVALID')
        return ''.join(x.text or '' for x in item.iter() if x.tag.rsplit('}',1)[-1] == 't'), raw
    if typ == 's':
        index = int(raw)
        if not 0 <= index < len(strings):
            raise ValueError('SHARED_STRING_MISSING')
        return strings[index], raw
    if typ == 'e':
        raise ValueError('EXCEL_ERROR_CELL')
    if typ == 'b':
        if raw not in ('0','1'):
            raise ValueError('BOOLEAN_CELL_INVALID')
        return 'TRUE' if raw == '1' else 'FALSE', raw
    if typ == 'd':
        value = datetime.fromisoformat(raw)
        if value.tzinfo or value.microsecond:
            raise ValueError('DATE_PRECISION_REVIEW')
        return value.strftime('%Y-%m-%d %H:%M:%S'), raw
    if typ not in ('n','str'):
        raise ValueError('CELL_TYPE_UNSUPPORTED')
    if raw and int(cell.attrib.get('s','0')) in dates and typ == 'n':
        return excel_date(raw,epoch1904), raw
    return raw, raw


def parse_source_workbook(raw: bytes):
    inspect_xlsx(raw)
    records, issues, sheet_summaries = [], [], []
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        workbook = safe_xml(archive.read('xl/workbook.xml'))
        props = workbook.find('s:workbookPr',NS)
        epoch1904 = props is not None and props.attrib.get('date1904','false').lower() in ('1','true')
        relpath = 'xl/_rels/workbook.xml.rels'
        if relpath not in archive.namelist():
            raise ValueError('WORKBOOK_RELATIONSHIPS_MISSING')
        rels = safe_xml(archive.read(relpath))
        targets = {node.attrib.get('Id'): node.attrib.get('Target') for node in rels if node.tag == '{'+REL+'}Relationship' and node.attrib.get('Type','').endswith('/worksheet')}
        strings, dates = _strings(archive), _styles(archive)
        sheets = workbook.findall('s:sheets/s:sheet',NS)
        if not 1 <= len(sheets) <= 30:
            raise ValueError('WORKSHEET_COUNT_LIMIT')
        total_cells = 0
        for sheet in sheets:
            name = sheet.attrib.get('name','')
            target = targets.get(sheet.attrib.get('{'+NS['r']+'}id'))
            if not name or not target:
                raise ValueError('WORKSHEET_RELATIONSHIP_INVALID')
            target = target.lstrip('/') if target.startswith('/xl/') else posixpath.normpath(posixpath.join('xl', target))
            if not target.startswith('xl/worksheets/') or '..' in target.split('/') or ':' in target or '\\' in target:
                raise ValueError('WORKSHEET_TARGET_INVALID')
            tree = safe_xml(archive.read(target))
            values, raw_cells, sheet_issues = {}, {}, []
            max_row, max_col = 0, 0
            for row in tree.findall('s:sheetData/s:row',NS):
                for cell in row.findall('s:c',NS):
                    total_cells += 1
                    if total_cells > MAX_CELLS:
                        raise ValueError('WORKBOOK_CELL_LIMIT')
                    reference = cell.attrib.get('r','')
                    r,c = cell_position(reference)
                    if (r,c) in raw_cells:
                        raise ValueError('DUPLICATE_CELL_REFERENCE')
                    max_row,max_col = max(max_row,r),max(max_col,c)
                    try:
                        value, raw_value = _cell(cell,strings,dates,epoch1904)
                    except (ValueError,InvalidOperation,OverflowError) as error:
                        value,raw_value = '',cell.findtext('s:v',default='',namespaces=NS)
                        code = str(error) if re.fullmatch('[A-Z_]+',str(error)) else 'CELL_VALUE_INVALID'
                        sheet_issues.append({'sheet':name,'row':r,'cell':reference,'code':code})
                    values[r,c] = value
                    raw_cells[r,c] = {'sheet':name,'cell':reference,'row':r,'column':c,'raw_stored_value':raw_value,'decoded_value':value,'cell_type':cell.attrib.get('t','n')}
            if max_row * max_col > MAX_CELLS:
                raise ValueError('WORKSHEET_SPARSE_EXPANSION_LIMIT')
            if not values:
                sheet_summaries.append({'sheet':name,'state':'EMPTY','source_rows':0,'record_count':0,'hidden':sheet.attrib.get('state')!='visible' and 'state' in sheet.attrib})
                continue
            stream = io.StringIO(newline='')
            writer = csv.writer(stream, lineterminator='\n')
            for r in range(1,max_row+1):
                writer.writerow([values.get((r,c),'') for c in range(1,max_col+1)])
            parsed = parse_source_table(stream.getvalue().encode('utf-8'))
            for record in parsed['records']:
                record['sheet'] = name
                record['source_format'] = 'xlsx'
                for field in record['fields'].values():
                    if 'column' in field:
                        cell_info = raw_cells.get((field['row'],field['column']))
                        if cell_info:
                            field.update(cell_info)
                    field['sheet'] = name
                records.append(record)
            for issue in parsed['issues']:
                issues.append({**issue,'sheet':name})
            issues.extend(sheet_issues)
            sheet_summaries.append({'sheet':name,'state':parsed['state'],'source_rows':parsed['source_rows'],
                                    'record_count':parsed['record_count'] if 'record_count' in parsed else 0,
                                    'document_kind':parsed['document_kind'],'metadata':parsed.get('metadata',{}),
                                    'controls':parsed['controls'],'hidden':sheet.attrib.get('state','visible')!='visible'})
    nonempty=[s for s in sheet_summaries if s['state']!='EMPTY']
    no_activity=not records and not issues and bool(nonempty) and all(s['state']=='NO_ACTIVITY_REPORTED' for s in nonempty)
    if not records and not no_activity:
        issues.append({'code':'NO_VALID_RECORDS','row':0})
    kinds = {record['kind'] for record in records} if records else {s['document_kind'] for s in nonempty}
    return {'parser_version':VERSION,'source_format':'xlsx','document_kind':next(iter(kinds)) if len(kinds)==1 else 'mixed_workbook',
            'state':'NO_ACTIVITY_REPORTED' if no_activity else 'NORMALIZED' if not issues else 'VALIDATION_REQUIRED','records':records,'record_count':len(records),
            'source_rows':sum(s['source_rows'] for s in sheet_summaries),'issues':issues[:200],'issue_count':len(issues),
            'sheets':sheet_summaries,'metadata':nonempty[0].get('metadata',{}) if len(nonempty)==1 else {},
            'date_system':'1904' if epoch1904 else '1900','institution_verified':False,'accounting_complete':False,'no_activity_declared':no_activity,'coverage_verified':False}
