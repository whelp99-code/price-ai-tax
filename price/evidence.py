"""Bounded original storage; PDF text extraction is optional, never OCR success."""
from __future__ import annotations
import base64
import binascii
import hashlib
from pathlib import PurePath
from .db import DomainError

MAX_BYTES = 2_000_000
MIME = {".txt": "text/plain", ".csv": "text/csv", ".json": "application/json", ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def decode_evidence(filename: str, encoded: str) -> dict:
    if "/" in filename or "\\" in filename or any(ord(ch) < 32 for ch in filename) or filename in (".", ".."):
        raise DomainError("UNSAFE_FILENAME", "경로나 제어문자가 없는 파일명으로 올리세요.", 422)
    suffix = PurePath(filename).suffix.lower()
    if suffix not in MIME:
        raise DomainError("UNSUPPORTED_FILE", "PDF·PNG·JPEG·TXT·CSV·JSON만 보관할 수 있습니다.", 422)
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise DomainError("INVALID_BASE64", "파일 인코딩이 잘못되었습니다.", 422)
    if not content or len(content) > MAX_BYTES:
        raise DomainError("FILE_SIZE", "파일은 1 byte 이상, 2 MB 이하여야 합니다.", 413)
    text, status = "", "STORED_ONLY"
    if suffix == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise DomainError("MIME_MISMATCH", "PDF 파일 서명이 맞지 않습니다.", 422)
        status = "PDF_STORED_EXTRACTION_PENDING"
    elif suffix == ".png":
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise DomainError("MIME_MISMATCH", "PNG 파일 서명이 맞지 않습니다.", 422)
        status = "IMAGE_STORED_OCR_NOT_RUN"
    elif suffix in (".jpg", ".jpeg"):
        if not content.startswith(b"\xff\xd8\xff"):
            raise DomainError("MIME_MISMATCH", "JPEG 파일 서명이 맞지 않습니다.", 422)
        status = "IMAGE_STORED_OCR_NOT_RUN"
    else:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise DomainError("ENCODING", "텍스트 파일은 UTF-8로 저장해 주세요.", 422)
        if "\x00" in text:
            raise DomainError("BINARY_TEXT", "바이너리 내용을 텍스트로 처리할 수 없습니다.", 422)
        if len(text) > 200_000:
            status, text = "TEXT_TRUNCATED_REVIEW", text[:200_000]
        else:
            status = "TEXT_EXTRACTED"
    return {"filename": filename, "mime": MIME[suffix], "content": content, "sha256": hashlib.sha256(content).hexdigest(), "text_content": text, "extraction_status": status}
