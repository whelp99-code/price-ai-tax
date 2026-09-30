from __future__ import annotations
import io
import struct
import zipfile
import pytest
from price.ingest_safety import inspect_document, inspect_xlsx, safe_xml, text_decode


def workbook(extra=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<Types/>')
        z.writestr('xl/workbook.xml', '<workbook/>')
        for name, content in (extra or {}).items():
            z.writestr(name, content)
    return stream.getvalue()


@pytest.mark.parametrize('encoding', ['utf-8-sig', 'cp949'])
def test_korean_table_strict_decode(encoding):
    raw = '거래일자,입금액,출금액\n2026-09-01,100,0\n'.encode(encoding)
    assert inspect_document('통장.csv', raw)['state'] == 'STRUCTURE_CHECKED'
    assert text_decode(raw)[0].startswith('거래일자')


@pytest.mark.parametrize('raw', [b'x\x00y', b'x\x1by', b'\xff\xff'])
def test_binary_is_not_silently_replaced(raw):
    assert inspect_document('input.csv', raw)['state'] == 'QUARANTINED'


@pytest.mark.parametrize('name,raw', [
    ('payload.csv', b'<script>not executed</script>'),
    ('payload.exe', b'MZexample'),
    ('report.pdf', b'not a PDF'),
    ('report.xlsx', b'not an archive'),
    ('photo.jpg', b'not an image'),
    ('photo.png', b'\x89PNG\r\n\x1a\n'),
    ('empty.csv', b''),
])
def test_mismatch_and_unknown_formats_quarantined(name, raw):
    result = inspect_document(name, raw)
    assert result['state'] == 'QUARANTINED'
    assert result['malware_certified'] is False


@pytest.mark.parametrize('xml', [
    b'<!DOCTYPE x [<!ENTITY a "x">]><x>&a;</x>',
    b'<!doctype x><x/>',
    b'<?xml-stylesheet href="remote"?><x/>',
    b'<x>\x00</x>',
    ('<x>' * 66 + '</x>' * 66).encode(),
])
def test_unsafe_xml_rejected(xml):
    assert inspect_document('invoice.xml', xml)['state'] == 'QUARANTINED'


def test_plain_xml_inspection_is_not_tax_validation():
    result = inspect_document('invoice.xml', b'<invoice><gross>1100</gross></invoice>')
    assert result['code'] == 'XML_READY'
    assert result['malware_certified'] is False


def test_xlsx_checked_but_not_claimed_extracted():
    result = inspect_document('bank.xlsx', workbook())
    assert result['code'] == 'TABLE_READER_REQUIRED'
    assert result['state'] == 'READER_REQUIRED'


@pytest.mark.parametrize('extra,code', [
    ({'../outside.txt': b'x'}, 'ARCHIVE_INVALID_PATH'),
    ({'xl/vbaProject.bin': b'not executable'}, 'ACTIVE_CONTENT_REVIEW'),
    ({'xl/externalLinks/link.xml': b'<x/>'}, 'ACTIVE_CONTENT_REVIEW'),
    ({'xl/_rels/workbook.xml.rels': b'<Relationships><Relationship TargetMode="External" Target="http://example.invalid"/></Relationships>'}, 'EXTERNAL_REFERENCE_REVIEW'),
    ({'large.txt': b'A' * 1_000_000}, 'ARCHIVE_RESOURCE_LIMIT'),
])
def test_archive_boundaries(extra, code):
    result = inspect_document('bank.xlsx', workbook(extra))
    assert result['state'] == 'QUARANTINED'
    assert result['code'] == code


def test_archive_symlink_is_not_followed():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as z:
        i = zipfile.ZipInfo('xl/link')
        i.external_attr = 0o120777 << 16
        z.writestr(i, 'outside')
    assert inspect_document('bank.xlsx', stream.getvalue())['code'] == 'ARCHIVE_LINK_NOT_ALLOWED'


def test_pdf_remains_reader_required():
    result = inspect_document('invoice.pdf', b'%PDF-1.7\nsynthetic-placeholder\n%%EOF')
    assert result['state'] == 'READER_REQUIRED'
    assert result['code'] == 'DOCUMENT_READER_REQUIRED'


def test_pdf_password_not_silently_ignored():
    result = inspect_document('invoice.pdf', b'%PDF-1.7\n/Encrypt 1 0 R\n%%EOF')
    assert result['code'] == 'PASSWORD_REQUIRED'


def test_image_pixel_budget():
    raw = b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + b'IHDR' + struct.pack('>II', 100000, 100000) + b'\0' * 9
    assert inspect_document('photo.png', raw)['code'] == 'IMAGE_RESOURCE_LIMIT'
