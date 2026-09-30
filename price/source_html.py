"""Read inert flat HTML table exports; never render or execute uploaded HTML.

Old spreadsheet exports sometimes contain HTML rather than an OLE workbook.
Unknown tables and merged data remain visible failures, not silent omissions.
"""
from __future__ import annotations
import csv
import io
import re
from html.parser import HTMLParser
from .ingest_safety import text_decode, MAX_TEXT_CELL
from .source_tables import parse_source_table


class _Tables(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables=[]; self.rows=None; self.row=None; self.cell=None
        self.prefix=[]; self.cells=0; self.nodes=0

    def handle_decl(self, decl):
        if not re.fullmatch(r'DOCTYPE\s+html',decl,re.I):
            raise ValueError('HTML_DECLARATION_REVIEW')

    def handle_starttag(self, tag, attrs):
        self.nodes+=1
        if self.nodes>100_000:
            raise ValueError('HTML_RESOURCE_LIMIT')
        if tag in {'script','iframe','object','embed','img','svg','math','link','form','input','video','audio','base'}:
            raise ValueError('ACTIVE_CONTENT_REVIEW')
        for key,value in attrs:
            if key.startswith('on') or key in {'src','href','action','http-equiv','srcdoc'}:
                raise ValueError('ACTIVE_CONTENT_REVIEW')
            if key=='style' and re.search(r'url\s*\(|expression\s*\(|@import|behavior\s*:|display\s*:\s*none|visibility\s*:\s*hidden',value or '',re.I):
                raise ValueError('ACTIVE_CONTENT_REVIEW')
            if key in {'rowspan','colspan'} and value!='1':
                raise ValueError('MERGED_TABLE_REVIEW')
        if tag=='table':
            if self.rows is not None:
                raise ValueError('NESTED_TABLE_REVIEW')
            self.rows=[]
        elif tag=='tr':
            if self.rows is None or self.row is not None:
                raise ValueError('HTML_ROW_STRUCTURE')
            self.row=[]
        elif tag in {'td','th'}:
            if self.row is None or self.cell is not None:
                raise ValueError('HTML_CELL_STRUCTURE')
            self.cell=[]
        elif tag=='br':
            (self.cell if self.cell is not None else self.prefix).append(' ')
        elif tag in {'p','div'} and self.rows is None:
            self.prefix.append('\n')

    def handle_endtag(self,tag):
        if tag in {'td','th'}:
            if self.cell is None or self.row is None:
                raise ValueError('HTML_CELL_STRUCTURE')
            value=''.join(self.cell).strip()
            if len(value)>MAX_TEXT_CELL:
                raise ValueError('HTML_RESOURCE_LIMIT')
            self.row.append(value);self.cell=None;self.cells+=1
            if self.cells>500_000 or len(self.row)>128:
                raise ValueError('HTML_RESOURCE_LIMIT')
        elif tag=='tr':
            if self.row is None or self.cell is not None or self.rows is None:
                raise ValueError('HTML_ROW_STRUCTURE')
            self.rows.append(self.row);self.row=None
            if len(self.rows)>20_100:
                raise ValueError('HTML_RESOURCE_LIMIT')
        elif tag=='table':
            if self.rows is None or self.row is not None:
                raise ValueError('HTML_TABLE_STRUCTURE')
            self.tables.append(self.rows);self.rows=None
            if len(self.tables)>100:
                raise ValueError('HTML_RESOURCE_LIMIT')
        elif tag in {'p','div'} and self.rows is None:
            self.prefix.append('\n')

    def handle_data(self,data):
        if re.search(r'@import|expression\s*\(|url\s*\(',data,re.I):
            raise ValueError('ACTIVE_CONTENT_REVIEW')
        if self.cell is not None:self.cell.append(data)
        elif self.rows is None:self.prefix.append(data)
        elif data.strip():raise ValueError('HTML_UNMAPPED_TEXT')


def read_html_tables(raw: bytes):
    if len(raw)>5_000_000:
        raise ValueError('HTML_RESOURCE_LIMIT')
    text,encoding=text_decode(raw)
    if not re.search(r'<(?:!doctype\s+html|html\b|table\b)',text[:512],re.I):
        raise ValueError('HTML_STRUCTURE_UNKNOWN')
    parser=_Tables();parser.feed(text);parser.close()
    if parser.rows is not None or parser.row is not None or parser.cell is not None or not parser.tables:
        raise ValueError('HTML_TABLE_STRUCTURE')
    return parser.tables,''.join(parser.prefix),encoding


def parse_source_html(raw: bytes):
    base={'records':[],'issues':[],'controls':[],'accounting_complete':False,
          'parser_version':'flat-html-source/2','institution_verified':False,'document_kind':'html_tables'}
    try:tables,prefix,encoding=read_html_tables(raw)
    except (ValueError,UnicodeError) as exc:
        code=str(exc) if re.fullmatch(r'[A-Z_]+',str(exc)) else 'HTML_READ_FAILED'
        return {**base,'state':'READ_FAILED','issues':[{'row':0,'code':code}]}
    records,issues,controls,details=[],[],[],[]
    metadata={};source_rows=0
    for index,rows in enumerate(tables,1):
        stream=io.StringIO(newline='');writer=csv.writer(stream,lineterminator='\n')
        source_row_map={};next_line=1
        for line in prefix.splitlines():
            if line.strip():writer.writerow([line.strip()]);next_line+=1
        for row_index,cells in enumerate(rows,1):
            source_row_map[next_line]=row_index
            row_stream=io.StringIO(newline='')
            csv.writer(row_stream,lineterminator='\n').writerow(cells)
            serialized=row_stream.getvalue()
            stream.write(serialized);next_line+=serialized.count('\n')
        result=parse_source_table(stream.getvalue().encode())
        for record in result['records']:
            record['sheet']='HTML-table-'+str(index)
            original_row=source_row_map.get(record['row'])
            for field in record['fields'].values():
                field.update(table=index,source_row=original_row,source_format='html_table')
            records.append(record)
        issues.extend({**issue,'table':index} for issue in result['issues'])
        controls.extend({**control,'table':index} for control in result['controls'])
        source_rows+=result.get('source_rows',0)
        metadata.update(result.get('metadata',{}))
        details.append({'table':index,'state':result['state'],'record_count':len(result['records']),'balance_checks':result.get('balance_checks',[])})
    no_activity=not records and not issues and bool(details) and all(d['state']=='NO_ACTIVITY_REPORTED' for d in details)
    return {**base,'state':'NO_ACTIVITY_REPORTED' if no_activity else 'NORMALIZED' if records and not issues else 'VALIDATION_REQUIRED',
            'encoding':encoding,'records':records,'record_count':len(records),'source_rows':source_rows,
            'metadata':metadata,'controls':controls,'control_total_present':bool(controls),
            'issues':issues[:200],'issue_count':len(issues),'tables':details,'no_activity_declared':no_activity,'coverage_verified':False}
