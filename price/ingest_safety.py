"""Bounded, non-executing inspection of uploaded source documents.

Inspection is not an antivirus certification. Complex formats stay in a
reader-required state until an isolated, tested reader is available.
"""
from __future__ import annotations
import io
import re
import stat
import struct
import zipfile
import zlib
from pathlib import PurePosixPath
from xml.etree import ElementTree
from .upload_models import MAX_FILE_BYTES

MAX_ARCHIVE_BYTES = 100_000_000
MAX_ARCHIVE_MEMBERS = 3000
MAX_XML_BYTES = 5_000_000
MAX_TEXT_CELL = 65_536


def text_decode(raw: bytes) -> tuple[str, str]:
    for encoding in ('utf-8-sig', 'cp949'):
        try:
            text = raw.decode(encoding, errors='strict')
        except UnicodeDecodeError:
            continue
        if any(ord(c) < 32 and c not in '\t\r\n' for c in text):
            raise ValueError('TEXT_CONTROL_CHARACTERS')
        return text, encoding
    raise ValueError('TEXT_ENCODING_UNSUPPORTED')


def safe_xml(raw: bytes):
    if len(raw) > MAX_XML_BYTES:
        raise ValueError('XML_RESOURCE_LIMIT')
    text = raw.decode('utf-8-sig', errors='strict')
    if '\x00' in text or re.search(r'<!\s*(?:DOCTYPE|ENTITY)|<\?xml-stylesheet', text, re.I):
        raise ValueError('XML_DECLARATION_NOT_ALLOWED')
    root = ElementTree.fromstring(text)
    count, pending = 0, [(root, 1)]
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > 200_000 or depth > 64 or len(node.text or '') > MAX_TEXT_CELL:
            raise ValueError('XML_RESOURCE_LIMIT')
        pending.extend((child, depth + 1) for child in node)
    return root


def inspect_xlsx(raw: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError('ARCHIVE_RESOURCE_LIMIT')
        names, total = set(), 0
        for item in members:
            name = item.filename
            parts = PurePosixPath(name).parts
            if name in names or name.startswith(('/', '\\')) or '\\' in name or '..' in parts or ':' in name or '\x00' in name:
                raise ValueError('ARCHIVE_INVALID_PATH')
            names.add(name)
            if stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError('ARCHIVE_LINK_NOT_ALLOWED')
            if item.flag_bits & 1:
                raise ValueError('PASSWORD_REQUIRED')
            total += item.file_size
            if total > MAX_ARCHIVE_BYTES or item.file_size > 20_000_000 or item.file_size > max(item.compress_size, 1) * 300:
                raise ValueError('ARCHIVE_RESOURCE_LIMIT')
            lower = name.lower()
            if any(token in lower for token in ('vbaproject', 'activex', 'externallinks', 'embeddings/', 'connections.xml', 'customui/', 'macrosheet')):
                raise ValueError('ACTIVE_CONTENT_REVIEW')
            if not item.is_dir():
                content = archive.read(item)
                if lower.endswith(('.xml', '.rels')):
                    root = safe_xml(content)
                    if any(str(node.attrib.get('TargetMode', '')).lower() == 'external' for node in root.iter()):
                        raise ValueError('EXTERNAL_REFERENCE_REVIEW')
        if not {'[Content_Types].xml', 'xl/workbook.xml'}.issubset(names):
            raise ValueError('XLSX_STRUCTURE_INVALID')


def inspect_document(filename: str, raw: bytes) -> dict:
    suffix = PurePosixPath(filename).suffix.lower()
    result = {'state': 'QUARANTINED', 'code': 'FORMAT_NOT_SUPPORTED', 'mime': 'application/octet-stream',
              'container': suffix.lstrip('.'), 'inspection': 'bounded-structure-check/1', 'malware_certified': False}
    if not raw or len(raw) > MAX_FILE_BYTES:
        return {**result, 'code': 'FILE_SIZE_INVALID'}
    try:
        if suffix in ('.csv', '.txt', '.tsv'):
            text, encoding = text_decode(raw)
            if re.search(r'<\s*(?:html|script|iframe|object)\b', text[:4096], re.I):
                raise ValueError('ACTIVE_CONTENT_REVIEW')
            return {**result, 'state': 'STRUCTURE_CHECKED', 'code': 'TEXT_READY',
                    'mime': 'text/csv' if suffix in ('.csv', '.tsv') else 'text/plain', 'encoding': encoding}
        if suffix == '.xml':
            safe_xml(raw)
            return {**result, 'state': 'STRUCTURE_CHECKED', 'code': 'XML_READY', 'mime': 'application/xml'}
        if suffix == '.xlsx':
            if not raw.startswith(b'PK\x03\x04'):
                raise ValueError('XLSX_SIGNATURE_INVALID')
            inspect_xlsx(raw)
            return {**result, 'state': 'READER_REQUIRED', 'code': 'TABLE_READER_REQUIRED',
                    'mime': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}
        if suffix == '.xls':
            if not raw.startswith(bytes.fromhex('D0CF11E0A1B11AE1')):
                from .source_html import read_html_tables
                read_html_tables(raw)
                return {**result, 'state': 'STRUCTURE_CHECKED', 'code': 'HTML_TABLE_READY', 'mime': 'text/html'}
            return {**result, 'state': 'READER_REQUIRED', 'code': 'LEGACY_TABLE_READER_REQUIRED'}
        if suffix == '.pdf':
            if not raw.startswith(b'%PDF-') or b'%%EOF' not in raw[-2048:]:
                raise ValueError('PDF_STRUCTURE_INVALID')
            code = 'PASSWORD_REQUIRED' if re.search(rb'/Encrypt\b', raw) else 'DOCUMENT_READER_REQUIRED'
            return {**result, 'state': 'READER_REQUIRED', 'code': code, 'mime': 'application/pdf'}
        if suffix == '.png':
            if not raw.startswith(b'\x89PNG\r\n\x1a\n') or len(raw) < 33 or raw[12:16] != b'IHDR':
                raise ValueError('IMAGE_STRUCTURE_INVALID')
            width, height = struct.unpack('>II', raw[16:24])
            if not width or not height or width * height > 40_000_000:
                raise ValueError('IMAGE_RESOURCE_LIMIT')
            return {**result, 'state': 'READER_REQUIRED', 'code': 'IMAGE_READER_REQUIRED', 'mime': 'image/png'}
        if suffix in ('.jpg', '.jpeg'):
            if not raw.startswith(b'\xff\xd8\xff') or not raw.endswith(b'\xff\xd9'):
                raise ValueError('IMAGE_STRUCTURE_INVALID')
            return {**result, 'state': 'READER_REQUIRED', 'code': 'IMAGE_READER_REQUIRED', 'mime': 'image/jpeg'}
    except (ValueError, UnicodeError, ElementTree.ParseError, zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError, OSError) as exc:
        code = str(exc) if re.fullmatch(r'[A-Z][A-Z0-9_]{2,70}', str(exc)) else 'DOCUMENT_STRUCTURE_INVALID'
        return {**result, 'code': code}
    return result
