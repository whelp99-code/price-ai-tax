"""Conservative text-layer PDF tables with explicit page/line provenance.

Only recognized bank/card/invoice headers and unambiguous separated cells are
accepted. Missing cells, wrapped rows, footers and unknown layouts are surfaced,
not guessed or omitted. Image OCR is intentionally a separate capability.
"""
from __future__ import annotations
import csv
import io
import re
from .source_tables import parse_source_table, _mapping


def parse_pdf_table_text(text: str) -> dict:
    if len(text) > 8_000_000:
        return {'state': 'READ_FAILED', 'records': [], 'issues': [{'code': 'PDF_TABLE_RESOURCE_LIMIT'}], 'accounting_complete': False}
    pages = text.split('\f')
    if pages and not pages[-1].strip():
        pages.pop()
    mapped_lines, source_lines = [], []
    header_seen = False
    for page, content in enumerate(pages, 1):
        for source_line, line in enumerate(content.splitlines(), 1):
            cells = re.split(r'(?: {2,}|\t+)', line.strip()) if line.strip() else []
            if _mapping(cells):
                header_seen = True
            mapped_lines.append(cells if header_seen else [line.strip()])
            source_lines.append({'page': page, 'source_line': source_line, 'raw_line': line})
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator='\n')
    for cells in mapped_lines:
        writer.writerow(cells)
    result = parse_source_table(stream.getvalue().encode('utf-8'))
    for record in result['records']:
        line_index = record['row'] - 1
        origin = source_lines[line_index] if 0 <= line_index < len(source_lines) else {}
        for field in record['fields'].values():
            field.update(page=origin.get('page'), source_line=origin.get('source_line'), source_format='pdf_text_layer')
        record['pdf_page'] = origin.get('page')
        record['pdf_line'] = origin.get('source_line')
    for issue in result['issues']:
        index = issue.get('row', 0) - 1
        if 0 <= index < len(source_lines):
            issue.update(page=source_lines[index]['page'], source_line=source_lines[index]['source_line'])
    return {**result, 'parser_version': 'pdf-separated-table/1+' + result['parser_version'],
            'extraction_method': 'poppler-text-layer-table', 'page_count': len(pages),
            'ocr_used': False, 'statutory_validated': False, 'institution_verified': False}
