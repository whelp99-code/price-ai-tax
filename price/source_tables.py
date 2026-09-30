"""Conservative original-table extraction with row/field provenance.

Reads document facts only. It does not infer deductible tax, accounts,
company ownership, or whether an inflow is revenue.
"""
from __future__ import annotations
import csv
import io
import re
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation
from .ingest_safety import text_decode, MAX_TEXT_CELL

PARSER_VERSION = 'korean-source-table/3'
MAX_ROWS = 20_000
MAX_COLUMNS = 128
MAX_CELLS = 500_000
ALIASES = {
    'counterparty_id': ('상대방사업자등록번호', '거래처사업자번호'),
    'counterparty_account': ('상대계좌번호', '상대방계좌번호'),
    'settlement_reference': ('청구참조번호', '결제대상승인번호'),
    'date': ('거래일자', '거래일시', '거래일', '승인일자', '승인일시', '이용일자', '작성일자', '작성일'),
    'issued_date': ('발급일자', '발급일시'),
    'transmitted_date': ('전송일자', '전송일시'),
    'description': ('거래내용', '적요', '내용', '표시내용', '상대방', '거래처', '가맹점명', '가맹점', '사용처'),
    'credit': ('입금액', '입금', '맡기신금액', '받으신금액'),
    'debit': ('출금액', '출금', '찾으신금액', '지급금액'),
    'balance': ('잔액', '거래후잔액', '거래후잔고'),
    'account': ('계좌번호', '계좌'),
    'card': ('카드번호', '카드'),
    'currency': ('통화', '통화코드', '화폐'),
    'net': ('공급가액', '공급금액'),
    'tax': ('세액', '부가세', '부가가치세'),
    'gross': ('합계금액', '총금액', '승인금액', '이용금액', '결제금액'),
    'reference': ('승인번호', '거래번호', '전자세금계산서승인번호', '세금계산서번호'),
    'supplier_id': ('공급자사업자등록번호', '공급자등록번호', '공급자사업자번호'),
    'supplier_name': ('공급자상호', '공급자상호법인명'),
    'buyer_id': ('공급받는자사업자등록번호', '공급받는자등록번호', '공급받는자사업자번호'),
    'buyer_name': ('공급받는자상호', '공급받는자상호법인명'),
    'status': ('승인취소구분', '거래구분', '처리상태', '취소여부', '전자세금계산서종류'),
    'original_reference': ('원거래번호', '당초승인번호', '수정원본승인번호'),
}


def header_key(value: str) -> str:
    value = unicodedata.normalize('NFKC', value).strip().lower()
    return re.sub(r'[\s_()\[\]/·.-]', '', value.replace('(원)', '').replace('(KRW)', '').replace('(krw)', ''))


LOOKUP = {header_key(alias): field for field, aliases in ALIASES.items() for alias in aliases}


def amount(value: str, *, blank_zero=False) -> str | None:
    value = unicodedata.normalize('NFKC', value).strip()
    if not value or value == '-':
        return '0' if blank_zero else None
    value = re.sub(r'^(?:₩|￦|KRW)\s*|\s*(?:원|KRW)$', '', value, flags=re.I).strip()
    negative = value.startswith('(') and value.endswith(')')
    if negative:
        value = '-' + value[1:-1]
    if not re.fullmatch(r'[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,6})?', value):
        raise ValueError('AMOUNT_INVALID')
    number = Decimal(value.replace(',', ''))
    if not number.is_finite() or abs(number) > Decimal('1000000000000000'):
        raise ValueError('AMOUNT_RANGE')
    return format(number, 'f')


def date_value(value: str) -> str:
    value = unicodedata.normalize('NFKC', value).strip()
    value = value.replace('년', '-').replace('월', '-').replace('일', '').strip()
    candidates = ('%Y-%m-%d', '%Y.%m.%d', '%Y/%m/%d', '%Y%m%d',
                  '%Y-%m-%d %H:%M:%S', '%Y.%m.%d %H:%M:%S', '%Y/%m/%d %H:%M:%S',
                  '%Y-%m-%d %H:%M', '%Y.%m.%d %H:%M', '%Y/%m/%d %H:%M')
    for fmt in candidates:
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.isoformat() if '%H' in fmt else parsed.date().isoformat()
        except ValueError:
            continue
    raise ValueError('DATE_INVALID')


def _mapping(row):
    found, repeated = {}, set()
    for col, label in enumerate(row):
        field = LOOKUP.get(header_key(label))
        if field:
            if field in found:
                repeated.add(field)
            found[field] = col
    if repeated:
        return None
    kinds = []
    if {'date', 'credit', 'debit'}.issubset(found):
        kinds.append('bank_statement')
    if {'date', 'net', 'tax'}.issubset(found) and ('reference' in found or {'supplier_id', 'buyer_id'}.issubset(found)):
        kinds.append('tax_invoice_list')
    if {'date', 'gross', 'description'}.issubset(found) and ('card' in found or 'reference' in found) and 'net' not in found:
        kinds.append('card_transactions')
    return (kinds[0], found) if len(kinds) == 1 else None


def _metadata(text):
    values = {}
    for field, pattern in {
        'account': r'(?:계좌번호)\s*[:：,\t]\s*([0-9*Xx-]{6,40})',
        'card': r'(?:카드번호)\s*[:：,\t]\s*([0-9*Xx-]{8,40})',
        'currency': r'(?:통화|화폐)\s*[:：,\t]\s*(KRW|USD|EUR|JPY|CNY)\b',
        'owner_name': r'(?:예금주|계좌명)\s*[:：,\t]\s*([^\r\n,\t]{1,120})',
    }.items():
        matches = list(dict.fromkeys(re.findall(pattern, text, re.I)))
        if len(matches) == 1:
            values[field] = matches[0].strip()
    if re.search(r'단위\s*[:：]\s*원\b', text):
        values.setdefault('currency', 'KRW')
    from .source_periods import printed_query_period
    values.update(printed_query_period(text))
    return values


def _issue(row, code, field=None):
    value = {'row': row, 'code': code}
    if field:
        value['field'] = field
    return value


def parse_source_table(raw: bytes) -> dict:
    from .opening_sources import try_parse_opening
    opening=try_parse_opening(raw)
    if opening is not None:return opening
    text, encoding = text_decode(raw)
    chosen = None
    for delimiter in (',', '\t', ';', '|'):
        reader = csv.reader(io.StringIO(text, newline=''), delimiter=delimiter, strict=True)
        try:
            for n, row in enumerate(reader):
                if n >= 60:
                    break
                mapped = _mapping(row)
                if mapped:
                    chosen = delimiter, n, row, mapped
                    break
        except csv.Error:
            continue
        if chosen:
            break
    base = {'parser_version': PARSER_VERSION, 'encoding': encoding, 'records': [], 'issues': [],
            'controls': [], 'omitted_rows': [], 'source_rows': 0, 'accounting_complete': False,
            'institution_verified': False}
    if not chosen:
        return {**base, 'state': 'FORMAT_REVIEW', 'document_kind': 'unknown', 'issues': [_issue(0, 'TABLE_HEADER_UNKNOWN')]}
    delimiter, header_index, header, (kind, fields) = chosen
    prefix = '\n'.join(text.splitlines()[:header_index + 1])
    metadata = _metadata(prefix)
    if any('(원)' in label or '(KRW)' in label.upper() for label in header):
        metadata.setdefault('currency', 'KRW')
    records, issues, controls, omitted = [], [], [], []
    no_activity_rows=[]
    from .source_periods import is_no_activity_line
    reader = csv.reader(io.StringIO(text, newline=''), delimiter=delimiter, strict=True)
    cells, candidate_rows, previous_line = 0, 0, 0
    try:
        for ordinal, row in enumerate(reader):
            line = previous_line + 1
            previous_line = reader.line_num
            if ordinal <= header_index:
                continue
            if ordinal-header_index>MAX_ROWS+100 or reader.line_num>MAX_ROWS*3:
                issues.append(_issue(line,'TABLE_RESOURCE_LIMIT'))
                break
            if not row or not any(x.strip() for x in row):
                omitted.append({'row': line, 'reason': 'blank'})
                continue
            if is_no_activity_line(row):
                no_activity_rows.append(line)
                continue
            cells += len(row)
            if candidate_rows >= MAX_ROWS or cells > MAX_CELLS or len(row) > MAX_COLUMNS or any(len(v) > MAX_TEXT_CELL for v in row):
                issues.append(_issue(line, 'TABLE_RESOURCE_LIMIT'))
                break
            if [header_key(x) for x in row] == [header_key(x) for x in header]:
                omitted.append({'row': line, 'reason': 'repeated_header'})
                continue
            if len(row) != len(header):
                issues.append(_issue(line, 'COLUMN_COUNT_MISMATCH'))
                candidate_rows += 1
                continue
            raw_fields = {field: row[col].strip() for field, col in fields.items()}
            first = next((v.strip() for v in row if v.strip()), '')
            if re.fullmatch(r'(?:총\s*)?합\s*계(?:\s*[:：]?)?', first):
                control = {'row': line, 'values': {}}
                try:
                    for field in ('credit', 'debit', 'net', 'tax', 'gross'):
                        if field in raw_fields:
                            parsed = amount(raw_fields[field])
                            if parsed is not None:
                                control['values'][field] = parsed
                    if not control['values']:
                        raise ValueError('TOTAL_ROW_EMPTY')
                    controls.append(control)
                except (ValueError, InvalidOperation) as exc:
                    issues.append(_issue(line, str(exc)))
                continue
            candidate_rows += 1
            values, evidence = {}, {}
            current_field = 'date'
            try:
                for field, value in raw_fields.items():
                    current_field = field
                    if value.startswith(('=', '@')) or re.match(r'^[+-][A-Za-z(]', value):
                        raise ValueError('CELL_FORMULA_REVIEW')
                    evidence[field] = {'row': line, 'column': fields[field] + 1, 'header': header[fields[field]], 'raw': value}
                    if field in ('credit', 'debit', 'net', 'tax', 'gross', 'balance'):
                        values[field] = amount(value, blank_zero=field in ('credit', 'debit'))
                    elif field in ('date', 'issued_date', 'transmitted_date'):
                        values[field] = date_value(value) if value or field == 'date' else None
                    else:
                        values[field] = value or None
                for field in ('currency', 'account', 'card'):
                    if not values.get(field):
                        values[field] = metadata.get(field)
                if values.get('currency'):
                    values['currency'] = values['currency'].upper()
                    if not re.fullmatch(r'[A-Z]{3}', values['currency']):
                        raise ValueError('CURRENCY_INVALID')
                if values.get('currency') == 'KRW':
                    for field in ('credit', 'debit', 'net', 'tax', 'gross', 'balance'):
                        if values.get(field) is not None and Decimal(values[field]) != Decimal(values[field]).to_integral_value():
                            raise ValueError('KRW_FRACTION_REVIEW')
                if kind == 'bank_statement':
                    credit, debit = Decimal(values['credit']), Decimal(values['debit'])
                    if credit < 0 or debit < 0 or (credit == 0) == (debit == 0):
                        raise ValueError('BANK_DIRECTION_AMBIGUOUS')
                    values['direction'] = 'receipt' if credit else 'payment'
                    values['gross'] = str(credit or debit)
                    values['tax'] = None
                    values['tax_inferred'] = False
                elif kind == 'tax_invoice_list':
                    if values.get('net') is None or values.get('tax') is None:
                        raise ValueError('INVOICE_AMOUNT_MISSING')
                    calculated = Decimal(values['net']) + Decimal(values['tax'])
                    if values.get('gross') is not None and Decimal(values['gross']) != calculated:
                        raise ValueError('INVOICE_TOTAL_MISMATCH')
                    if values.get('gross') is None:
                        values['gross'] = str(calculated)
                        evidence['gross'] = {'derivation': 'net+tax', 'row': line, 'inputs': ['net', 'tax']}
                    values['direction'] = None  # requires confirmed company identity
                else:
                    if values.get('gross') is None:
                        raise ValueError('CARD_AMOUNT_MISSING')
                    values['tax'] = None
                    values['tax_inferred'] = False
                    values['direction'] = 'card_event'
                records.append({'row': line, 'kind': kind, 'values': values, 'fields': evidence,
                                'context_pending': ['company_ownership', 'accounting_treatment'] + ([] if values.get('currency') else ['currency'])})
            except (ValueError, InvalidOperation) as exc:
                code = str(exc) if re.fullmatch(r'[A-Z_]+', str(exc)) else 'ROW_INVALID'
                issues.append(_issue(line, code, current_field))
    except csv.Error:
        issues.append(_issue(reader.line_num, 'CSV_SYNTAX'))
    from .source_controls import bank_balance_checks, total_currency_conflict
    balance_checks, balance_issues = bank_balance_checks(records)
    issues.extend(balance_issues)
    for control in controls:
        control['checks'] = {}
        if total_currency_conflict(records):
            issues.append(_issue(control['row'], 'CONTROL_CURRENCY_AMBIGUOUS', 'currency'))
            control['status'] = 'CURRENCY_REVIEW'
            continue
        for field, source in control['values'].items():
            available = [r['values'].get(field) for r in records]
            if any(value is None for value in available):
                control['checks'][field] = {'status': 'UNVERIFIABLE'}
                issues.append(_issue(control['row'], 'CONTROL_TOTAL_UNVERIFIABLE', field))
            else:
                calculated = sum((Decimal(value) for value in available), Decimal(0))
                difference = calculated - Decimal(source)
                control['checks'][field] = {'source': source, 'calculated': str(calculated), 'difference': str(difference)}
                if difference != 0:
                    issues.append(_issue(control['row'], 'CONTROL_TOTAL_MISMATCH', field))
    if no_activity_rows and (records or candidate_rows):
        issues.append(_issue(no_activity_rows[0], 'NO_ACTIVITY_CONFLICT'))
    if not records and no_activity_rows and not candidate_rows and not issues and metadata.get('declared_period') and (metadata.get('account') or metadata.get('card')):
        return {**base,'state':'NO_ACTIVITY_REPORTED','document_kind':kind,'metadata':metadata,'records':[],'record_count':0,'source_rows':0,'issues':[],'no_activity_declared':True,'no_activity_rows':no_activity_rows,'coverage_verified':False}
    if not records:
        issues.append(_issue(0, 'NO_VALID_RECORDS'))
    return {**base, 'state': 'NORMALIZED' if not issues else 'VALIDATION_REQUIRED', 'document_kind': kind,
            'metadata': metadata, 'records': records, 'record_count': len(records),
            'source_rows': candidate_rows, 'issues': issues[:200], 'issue_count': len(issues),
            'controls': controls, 'control_total_present': bool(controls), 'balance_checks': balance_checks, 'omitted_rows': omitted[:200]}
