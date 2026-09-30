"""Read source facts from the KEC TaxInvoice XML vocabulary.

Structure reference: KICA SecuTAX NXS developer documentation,
https://developers.kica.co.kr/pki/secutax-nxs (checked 2026-09-28).
No schemas, links, transforms, signatures or document instructions execute.
This is not a signature verifier, statutory validator or tax decision engine.
"""
from __future__ import annotations
import re
from decimal import Decimal
from xml.etree import ElementTree
from .ingest_safety import safe_xml
from .source_tables import amount, date_value

TAX_NAMESPACE = 'urn:kr:or:kec:standard:Tax:ReusableAggregateBusinessInformationEntitySchemaModule:1:0'
PARSER_VERSION = 'kec-tax-invoice-facts/1'
SIGNATURE_NAMESPACE = 'http://www.w3.org/2000/09/xmldsig#'


def parse_tax_invoice_xml(raw: bytes) -> dict:
    base = {'parser_version': PARSER_VERSION, 'document_kind': 'tax_invoice_list',
            'records': [], 'issues': [], 'controls': [], 'source_rows': 0,
            'institution_verified': False, 'signature_verified': False,
            'signature_present': False, 'statutory_validated': False, 'accounting_complete': False}
    try:
        root = safe_xml(raw)
    except (ValueError, UnicodeError, ElementTree.ParseError):
        return {**base, 'state': 'READ_FAILED', 'issues': [{'row': 0, 'code': 'XML_UNSAFE_OR_INVALID'}]}
    if root.tag != '{' + TAX_NAMESPACE + '}TaxInvoice':
        return {**base, 'state': 'FORMAT_REVIEW', 'issues': [{'row': 0, 'code': 'XML_PROFILE_UNKNOWN'}]}
    base['signature_present'] = any(n.tag == '{' + SIGNATURE_NAMESPACE + '}Signature' for n in root.iter())
    issues, fields, values, controls = [], {}, {}, []

    def issue(code, path, row=1):
        if len(issues) < 200:
            issues.append({'row': row, 'code': code, 'path': path})

    def select(start, path, *, required=False, many=False, prefix='/TaxInvoice'):
        nodes = [start]
        for component in path.split('/'):
            found = []
            for node in nodes:
                siblings = [n for n in node if isinstance(n.tag, str) and n.tag.rsplit('}', 1)[-1] == component]
                if any(n.tag != '{' + TAX_NAMESPACE + '}' + component for n in siblings):
                    issue('XML_NAMESPACE_CONFLICT', prefix + '/' + path)
                found.extend(n for n in siblings if n.tag == '{' + TAX_NAMESPACE + '}' + component)
            nodes = found
            if not nodes:
                break
        if not many and len(nodes) > 1:
            issue('XML_AMBIGUOUS_FIELD', prefix + '/' + path)
            return None
        if required and not nodes:
            issue('XML_REQUIRED_FIELD', prefix + '/' + path)
        return nodes if many else (nodes[0] if nodes else None)

    def scalar(start, path, *, key=None, required=False, convert=None, prefix='/TaxInvoice', target=None):
        node = select(start, path, required=required, prefix=prefix)
        if node is None:
            return None
        fullpath = prefix + '/' + path
        text = (node.text or '').strip()
        if len(node) or len(text) > 2000:
            issue('XML_SCALAR_INVALID', fullpath)
            return None
        if not text:
            if required:
                issue('XML_REQUIRED_FIELD', fullpath)
            return None
        evidence = {'path': fullpath, 'namespace': TAX_NAMESPACE, 'raw': text, 'row': 1, 'source_format': 'xml'}
        if key:
            (fields if target is None else target)[key] = evidence
        try:
            return convert(text) if convert else text
        except (ValueError, ArithmeticError):
            code = 'DATE_INVALID' if convert is date_value else 'AMOUNT_INVALID'
            issue(code, fullpath)
            return None

    doc = select(root, 'TaxInvoiceDocument', required=True)
    settlement = select(root, 'TaxInvoiceTradeSettlement', required=True)
    if doc is None or settlement is None:
        return {**base, 'state': 'VALIDATION_REQUIRED', 'issues': issues}
    dp = '/TaxInvoice/TaxInvoiceDocument'
    sp = '/TaxInvoice/TaxInvoiceTradeSettlement'
    values['reference'] = scalar(doc, 'IssueID', key='reference', required=True, prefix=dp)
    values['date'] = scalar(doc, 'IssueDateTime', key='date', required=True, convert=date_value, prefix=dp)
    values['type_code'] = scalar(doc, 'TypeCode', key='type_code', prefix=dp)
    values['amendment_code'] = scalar(doc, 'AmendmentStatusCode', key='amendment_code', prefix=dp)
    values['purpose_code'] = scalar(doc, 'PurposeCode', key='purpose_code', prefix=dp)
    values['description'] = scalar(doc, 'DescriptionText', key='description', prefix=dp)
    values['original_reference'] = scalar(root, 'ExchangedDocument/ReferencedDocument/ID', key='original_reference')
    values['original_reference_type'] = 'referenced_document_id' if values['original_reference'] else None
    # A referenced exchange-document ID is not assumed to be a verified prior tax IssueID.
    for party, role in (('InvoicerParty', 'supplier'), ('InvoiceeParty', 'buyer')):
        node = select(settlement, party, required=True, prefix=sp)
        if node is None:
            continue
        prefix = sp + '/' + party
        ident = scalar(node, 'ID', key=role + '_id', required=True, prefix=prefix)
        values[role + '_id'] = ident.replace('-', '') if ident else None
        values[role + '_name'] = scalar(node, 'NameText', key=role + '_name', prefix=prefix)
        if ident and not re.fullmatch(r'(?:[0-9]{10}|[0-9]{3}-[0-9]{2}-[0-9]{5})', ident):
            issue('PARTY_IDENTIFIER_REVIEW', prefix + '/ID')
    summary = select(settlement, 'SpecifiedMonetarySummation', required=True, prefix=sp)
    currencies = set()
    if summary is not None:
        prefix = sp + '/SpecifiedMonetarySummation'
        for field, tag in (('net', 'ChargeTotalAmount'), ('tax', 'TaxTotalAmount'), ('gross', 'GrandTotalAmount')):
            values[field] = scalar(summary, tag, key=field, required=True, convert=amount, prefix=prefix)
            node = select(summary, tag, prefix=prefix)
            if node is not None and node.attrib.get('currencyID'):
                code = node.attrib['currencyID']
                if not re.fullmatch(r'[A-Z]{3}', code):
                    issue('CURRENCY_INVALID', prefix + '/' + tag)
                currencies.add(code)
    if len(currencies) > 1:
        issue('CURRENCY_CONFLICT', sp)
    values['currency'] = next(iter(currencies)) if len(currencies) == 1 else None
    if all(values.get(x) is not None for x in ('net', 'tax', 'gross')):
        difference = Decimal(values['net']) + Decimal(values['tax']) - Decimal(values['gross'])
        controls.append({'kind': 'net_plus_tax_equals_gross', 'difference': str(difference), 'source_path': sp + '/SpecifiedMonetarySummation'})
        if difference:
            issue('INVOICE_TOTAL_MISMATCH', sp)
        if values['currency'] == 'KRW' and any(Decimal(values[x]) != Decimal(values[x]).to_integral_value() for x in ('net', 'tax', 'gross')):
            issue('KRW_FRACTION_REVIEW', sp)
    lines = select(root, 'TaxInvoiceTradeLineItem', many=True)
    items, sequences = [], set()
    if len(lines) > 5000:
        issue('LINE_RESOURCE_LIMIT', '/TaxInvoice/TaxInvoiceTradeLineItem')
        lines = []
    for index, line in enumerate(lines, 1):
        prefix = f'/TaxInvoice/TaxInvoiceTradeLineItem[{index}]'
        item_fields = {}
        sequence = scalar(line, 'SequenceNumeric', required=True, prefix=prefix)
        if sequence in sequences:
            issue('LINE_SEQUENCE_CONFLICT', prefix, index)
        sequences.add(sequence)
        item = {'sequence': sequence, 'fields': item_fields,
                'net': scalar(line, 'InvoiceAmount', key='net', required=True, convert=amount, prefix=prefix, target=item_fields),
                'tax': scalar(line, 'TotalTax/CalculatedAmount', key='tax', required=True, convert=amount, prefix=prefix, target=item_fields),
                'description': scalar(line, 'NameText', key='description', prefix=prefix, target=item_fields)}
        items.append(item)
    for field in ('net', 'tax'):
        if items and values.get(field) is not None and all(item[field] is not None for item in items):
            calculated = sum((Decimal(item[field]) for item in items), Decimal(0))
            difference = calculated - Decimal(values[field])
            controls.append({'kind': 'line_sum_' + field, 'calculated': str(calculated), 'source': values[field], 'difference': str(difference)})
            if difference:
                issue('LINE_TOTAL_MISMATCH', '/TaxInvoice/TaxInvoiceTradeLineItem')
    values.update(direction=None, tax_inferred=False)
    record = {'row': 1, 'kind': 'tax_invoice_list', 'values': values, 'fields': fields, 'line_items': items,
              'context_pending': ['company_ownership', 'accounting_treatment', 'signature_validation'] + ([] if values['currency'] else ['currency'])}
    return {**base, 'state': 'VALIDATION_REQUIRED' if issues else 'NORMALIZED', 'records': [record],
            'record_count': 1, 'source_rows': 1, 'issues': issues, 'controls': controls,
            'control_total_present': bool(controls), 'line_count': len(items),
            'metadata': {'currency': values['currency']}, 'profile_reference': 'KICA-SecuTAX-NXS-KEC-example',
            'notice': 'XML의 표시 내용을 읽은 결과입니다. 진위·전자서명·국세청 접수·세무 판단은 검증하지 않았습니다.'}
