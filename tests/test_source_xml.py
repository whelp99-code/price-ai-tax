"""Synthetic values in the publicly documented KEC TaxInvoice XML shape.
Structure reference: https://developers.kica.co.kr/pki/secutax-nxs (2026-09-28).
No real taxpayer information, signature, filing or statutory validation.
"""
from __future__ import annotations
import pytest
from price.source_xml import parse_tax_invoice_xml, TAX_NAMESPACE
from price.ingestion import Ingestion
from test_source_ingestion import upload


def invoice_xml(net='1000', tax='100', gross='1100', extra='', line_net='1000', line_tax='100', currency=''):
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<TaxInvoice xmlns="{TAX_NAMESPACE}">
 <ExchangedDocument><ID>SYNTHETIC-DOC</ID><IssueDateTime>20260901120000</IssueDateTime></ExchangedDocument>
 <TaxInvoiceDocument><IssueID>SYNTHETIC-INVOICE-01</IssueID><TypeCode>0101</TypeCode><IssueDateTime>20260901</IssueDateTime>{extra}</TaxInvoiceDocument>
 <TaxInvoiceTradeSettlement>
  <InvoicerParty><ID>1111111111</ID><NameText>시험 공급자</NameText><SpecifiedOrganization><TaxRegistrationID>0001</TaxRegistrationID></SpecifiedOrganization></InvoicerParty>
  <InvoiceeParty><ID>2222222222</ID><NameText>시험 구매자</NameText></InvoiceeParty>
  <SpecifiedMonetarySummation><ChargeTotalAmount{currency}>{net}</ChargeTotalAmount><TaxTotalAmount{currency}>{tax}</TaxTotalAmount><GrandTotalAmount{currency}>{gross}</GrandTotalAmount></SpecifiedMonetarySummation>
 </TaxInvoiceTradeSettlement>
 <TaxInvoiceTradeLineItem><SequenceNumeric>1</SequenceNumeric><NameText>검증용 용역</NameText><InvoiceAmount>{line_net}</InvoiceAmount><TotalTax><CalculatedAmount>{line_tax}</CalculatedAmount></TotalTax></TaxInvoiceTradeLineItem>
</TaxInvoice>'''.encode()


def test_exact_document_facts_without_tax_or_identity_approval():
    result=parse_tax_invoice_xml(invoice_xml())
    assert result['state']=='NORMALIZED',result
    row=result['records'][0]
    assert row['values']['net']=='1000' and row['values']['tax']=='100' and row['values']['gross']=='1100'
    assert row['values']['supplier_id']=='1111111111' and row['values']['buyer_id']=='2222222222'
    assert row['values']['date']=='2026-09-01' and row['values']['direction'] is None
    assert row['values']['currency'] is None and 'deductible' not in row['values']
    assert row['fields']['net']['path'].endswith('/SpecifiedMonetarySummation/ChargeTotalAmount')
    assert result['signature_verified'] is False and result['institution_verified'] is False
    assert result['accounting_complete'] is False and result['controls'][0]['difference']=='0'


def test_declared_currency_preserved_not_assumed():
    result=parse_tax_invoice_xml(invoice_xml(currency=' currencyID="KRW"'))
    assert result['records'][0]['values']['currency']=='KRW'


def test_zero_tax_does_not_assert_exempt_or_deductible():
    r=parse_tax_invoice_xml(invoice_xml(tax='0',gross='1000',line_tax='0'))
    assert r['state']=='NORMALIZED'
    assert 'tax_category' not in r['records'][0]['values']


def test_negative_amendment_and_original_reference_preserved():
    extra='<AmendmentStatusCode>01</AmendmentStatusCode>'
    raw=invoice_xml('-1000','-100','-1100',extra,'-1000','-100').replace(b'</ExchangedDocument>', b'<ReferencedDocument><ID>SYNTHETIC-PRIOR</ID></ReferencedDocument></ExchangedDocument>')
    r=parse_tax_invoice_xml(raw)
    assert r['state']=='NORMALIZED' and r['records'][0]['values']['gross']=='-1100'
    assert r['records'][0]['values']['amendment_code']=='01'
    assert r['records'][0]['values']['original_reference']=='SYNTHETIC-PRIOR'


@pytest.mark.parametrize('change,code',[
    (lambda x:x.replace(b'<GrandTotalAmount>1100',b'<GrandTotalAmount>1200'),'INVOICE_TOTAL_MISMATCH'),
    (lambda x:x.replace(b'<InvoiceAmount>1000',b'<InvoiceAmount>990'),'LINE_TOTAL_MISMATCH'),
    (lambda x:x.replace(b'<TaxTotalAmount>100</TaxTotalAmount>',b''),'XML_REQUIRED_FIELD'),
    (lambda x:x.replace(b'<IssueID>SYNTHETIC-INVOICE-01</IssueID>',b'<IssueID>A</IssueID><IssueID>B</IssueID>'),'XML_AMBIGUOUS_FIELD'),
    (lambda x:x.replace(b'20260901</IssueDateTime>',b'20260230</IssueDateTime>'),'DATE_INVALID'),
    (lambda x:x.replace(b'<TaxTotalAmount>100',b'<TaxTotalAmount>NaN'),'AMOUNT_INVALID'),
    (lambda x:x.replace(b'<InvoiceAmount>1000</InvoiceAmount>',b''),'XML_REQUIRED_FIELD'),
])
def test_invalid_values_stay_candidates_with_errors(change,code):
    result=parse_tax_invoice_xml(change(invoice_xml()))
    assert result['state']=='VALIDATION_REQUIRED'
    assert any(e['code']==code for e in result['issues']),result
    assert result['accounting_complete'] is False


def test_other_namespace_is_not_local_name_accepted():
    r=parse_tax_invoice_xml(invoice_xml().replace(TAX_NAMESPACE.encode(),b'urn:unrelated'))
    assert r['state']=='FORMAT_REVIEW' and not r['records']


def test_foreign_namespace_duplicate_field_rejected():
    raw=invoice_xml().replace(b'<IssueID>SYNTHETIC-INVOICE-01</IssueID>',b'<IssueID>SYNTHETIC-INVOICE-01</IssueID><x:IssueID xmlns:x="urn:foreign">EVIL</x:IssueID>')
    r=parse_tax_invoice_xml(raw)
    assert r['state']=='VALIDATION_REQUIRED' and any(x['code']=='XML_NAMESPACE_CONFLICT' for x in r['issues'])


def test_nested_document_is_not_unwrapped():
    raw=invoice_xml().split(b'?>',1)[1]
    r=parse_tax_invoice_xml(b'<Envelope>'+raw+b'</Envelope>')
    assert r['state']=='FORMAT_REVIEW'


def test_dtd_and_external_entity_do_not_run():
    raw=b'<!DOCTYPE TaxInvoice [<!ENTITY a SYSTEM "https://example.invalid/secret">]>'+invoice_xml().split(b'?>',1)[1]
    r=parse_tax_invoice_xml(raw)
    assert r['state']=='READ_FAILED' and not r['records']


def test_duplicate_line_sequence_is_not_silently_merged():
    raw=invoice_xml()
    line=raw.split(b'<TaxInvoiceTradeLineItem>')[1].split(b'</TaxInvoiceTradeLineItem>')[0]
    raw=raw.replace(b'</TaxInvoice>',b'<TaxInvoiceTradeLineItem>'+line+b'</TaxInvoiceTradeLineItem></TaxInvoice>')
    r=parse_tax_invoice_xml(raw)
    assert r['state']=='VALIDATION_REQUIRED' and any(x['code']=='LINE_SEQUENCE_CONFLICT' for x in r['issues'])


def test_signature_presence_is_never_signature_validity():
    raw=invoice_xml().replace(b'</TaxInvoice>',b'<ds:Signature xmlns:ds="http://www.w3.org/2000/09/xmldsig#"><ds:SignatureValue>NOT-A-SIGNATURE</ds:SignatureValue></ds:Signature></TaxInvoice>')
    r=parse_tax_invoice_xml(raw)
    assert r['signature_present'] is True and r['signature_verified'] is False


def test_xml_upload_enters_real_ingestion_and_creates_source_case(w):
    b=upload(w,invoice_xml(),filename='세금계산서.xml')
    reader=Ingestion(w.svc); r=reader.run_once(w.company)
    assert r['state']=='SUCCEEDED' and r['cases']==1,r
    doc=b['files'][0]['document']['id']
    row=reader.records(w.owner,w.company,doc)['rows'][0]
    assert row['values']['gross']=='1100' and row['validation']=='NORMALIZED'
    with w.db.connect() as c:
        assert not w.db.list(c,w.company,'journal')
        assert w.db.verify_audit(c,w.company)['valid']
