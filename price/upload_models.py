"""Upload metadata validation for V01. No file access or external execution.

These models validate declared metadata, not the safety or content of a file.
The upload service must authenticate the actor, check company membership,
measure received bytes, inspect content, and verify the server-side digest.
"""
from __future__ import annotations

import unicodedata
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator

MAX_FILE_BYTES = 50_000_000
MAX_BATCH_BYTES = 500_000_000
MAX_BATCH_FILES = 100
MAX_CHUNK_BYTES = 1_000_000

FileId = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
Digest = Annotated[StrictStr, Field(pattern=r"^[a-fA-F0-9]{64}$")]
Revision = Annotated[StrictInt, Field(ge=1)]


class UploadInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)


class UploadFileInput(UploadInput):
    client_file_id: FileId
    filename: Annotated[StrictStr, Field(min_length=1, max_length=160)]
    size_bytes: Annotated[StrictInt, Field(gt=0, le=MAX_FILE_BYTES)]
    sha256: Digest

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        if value != value.strip() or value.startswith(".") or value.endswith("."):
            raise ValueError("파일명 앞뒤 공백이나 점은 허용하지 않습니다.")
        if any(ch in '/\\:' or unicodedata.category(ch).startswith("C") for ch in value):
            raise ValueError("경로나 제어 문자가 포함된 파일명은 허용하지 않습니다.")
        if len(value.encode("utf-8")) > 240:
            raise ValueError("파일명이 너무 깁니다.")
        return value

    @field_validator("sha256")
    @classmethod
    def canonical_digest(cls, value: str) -> str:
        return value.lower()

    @property
    def chunk_count(self) -> int:
        return (self.size_bytes + MAX_CHUNK_BYTES - 1) // MAX_CHUNK_BYTES

    def expected_chunk_bytes(self, index: int) -> int:
        if type(index) is not int or not 0 <= index < self.chunk_count:
            raise ValueError("유효하지 않은 청크 번호입니다.")
        return min(MAX_CHUNK_BYTES, self.size_bytes - index * MAX_CHUNK_BYTES)


class UploadBatchInput(UploadInput):
    idempotency_key: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")]
    files: Annotated[list[UploadFileInput], Field(min_length=1, max_length=MAX_BATCH_FILES)]

    @model_validator(mode="after")
    def validate_batch(self) -> UploadBatchInput:
        identifiers = [item.client_file_id for item in self.files]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("배치 안에서 파일 식별자가 중복되었습니다.")
        if sum(item.size_bytes for item in self.files) > MAX_BATCH_BYTES:
            raise ValueError("한 번에 올리는 자료의 총 크기 제한을 초과했습니다.")
        return self


class CompleteUploadInput(UploadInput):
    expected_revision: Revision


class CancelUploadInput(UploadInput):
    expected_revision: Revision
    reason: Annotated[StrictStr, Field(min_length=1, max_length=500)]

    @field_validator("reason")
    @classmethod
    def nonempty_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("중단 사유를 입력하세요.")
        return value
