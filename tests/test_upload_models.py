"""Pure input-contract tests; no company data or external services are used."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from price.upload_models import (
    CancelUploadInput, CompleteUploadInput, MAX_BATCH_BYTES, MAX_BATCH_FILES,
    MAX_CHUNK_BYTES, MAX_FILE_BYTES, UploadBatchInput, UploadFileInput,
)


def file_data(**changes):
    return dict(client_file_id="file_001", filename="통장내역_2026.csv",
                size_bytes=1234, sha256="ab" * 32) | changes


def batch_data(files=None, **changes):
    return dict(idempotency_key="batch_20260928", files=files or [file_data()]) | changes


def test_valid_korean_filename_and_digest():
    item = UploadFileInput(**file_data(sha256="AB" * 32))
    assert item.filename == "통장내역_2026.csv"
    assert item.sha256 == "ab" * 32
    assert item.size_bytes == 1234


@pytest.mark.parametrize("size", [True, False, "1234", 1.0, 0, -1, MAX_FILE_BYTES + 1, None])
def test_size_must_be_a_bounded_positive_integer(size):
    with pytest.raises(ValidationError):
        UploadFileInput(**file_data(size_bytes=size))


@pytest.mark.parametrize("filename", [
    "", ".", "..", ".hidden.csv", "../report.csv", "/report.csv",
    "folder/report.csv", "folder\\report.csv", "C:report.csv",
    "report\x00.csv", "report\n.csv", "report\r.csv", "report\t.csv",
    "report\u202e.csv", "report\u200b.csv", "report\ud800.csv",
    " report.csv", "report.csv ", "report.", "가" * 81 + ".csv",
    "a" * 161,
])
def test_filename_is_a_display_name_not_a_path(filename):
    with pytest.raises(ValidationError):
        UploadFileInput(**file_data(filename=filename))


@pytest.mark.parametrize("digest", ["", "a" * 63, "a" * 65, "g" * 64, "a" * 64 + "\n", 123, None])
def test_digest_shape(digest):
    with pytest.raises(ValidationError):
        UploadFileInput(**file_data(sha256=digest))


@pytest.mark.parametrize("field", ["actor_id", "company_id", "approved_by", "state", "storage_path"])
def test_client_cannot_supply_server_owned_fields(field):
    with pytest.raises(ValidationError):
        UploadFileInput(**file_data(**{field: "client_value"}))
    with pytest.raises(ValidationError):
        UploadBatchInput(**batch_data(**{field: "client_value"}))


@pytest.mark.parametrize("identifier", ["", "../file", "folder/file", "file id", "a" * 65, 1, None])
def test_file_identifier_format(identifier):
    with pytest.raises(ValidationError):
        UploadFileInput(**file_data(client_file_id=identifier))


def test_duplicate_file_ids_are_rejected():
    with pytest.raises(ValidationError):
        UploadBatchInput(**batch_data(files=[file_data(), file_data(filename="card.csv")]))


def test_identical_hashes_are_not_silently_deleted():
    batch = UploadBatchInput(**batch_data(files=[file_data(), file_data(client_file_id="file_002")]))
    assert len(batch.files) == 2
    assert batch.files[0].sha256 == batch.files[1].sha256


def test_empty_batch_is_rejected():
    with pytest.raises(ValidationError):
        UploadBatchInput(idempotency_key="batch_empty", files=[])


def test_batch_count_boundary():
    files = [file_data(client_file_id=f"file_{i}", size_bytes=1) for i in range(MAX_BATCH_FILES)]
    assert len(UploadBatchInput(**batch_data(files=files)).files) == MAX_BATCH_FILES
    with pytest.raises(ValidationError):
        UploadBatchInput(**batch_data(files=files + [file_data(client_file_id="one_extra")]))


def test_batch_total_boundary_without_allocating_file_payloads():
    files = [file_data(client_file_id=f"file_{i}", size_bytes=MAX_FILE_BYTES)
             for i in range(MAX_BATCH_BYTES // MAX_FILE_BYTES)]
    batch = UploadBatchInput(**batch_data(files=files))
    assert sum(item.size_bytes for item in batch.files) == MAX_BATCH_BYTES
    with pytest.raises(ValidationError):
        UploadBatchInput(**batch_data(files=files + [file_data(client_file_id="overflow", size_bytes=1)]))


@pytest.mark.parametrize("key", ["short", "a" * 129, "batch/invalid", "batch key", None])
def test_idempotency_key_format(key):
    with pytest.raises(ValidationError):
        UploadBatchInput(**batch_data(idempotency_key=key))


@pytest.mark.parametrize("size,expected", [
    (1, [1]), (MAX_CHUNK_BYTES, [MAX_CHUNK_BYTES]),
    (MAX_CHUNK_BYTES + 1, [MAX_CHUNK_BYTES, 1]),
    (MAX_CHUNK_BYTES * 2, [MAX_CHUNK_BYTES, MAX_CHUNK_BYTES]),
])
def test_chunk_lengths_preserve_declared_size(size, expected):
    item = UploadFileInput(**file_data(size_bytes=size))
    lengths = [item.expected_chunk_bytes(i) for i in range(item.chunk_count)]
    assert lengths == expected
    assert sum(lengths) == size


@pytest.mark.parametrize("index", [-1, 1, True, 0.0, "0", None])
def test_invalid_chunk_index(index):
    item = UploadFileInput(**file_data())
    with pytest.raises(ValueError):
        item.expected_chunk_bytes(index)


@pytest.mark.parametrize("revision", [0, -1, True, 1.0, "1", None])
def test_revision_is_strict(revision):
    with pytest.raises(ValidationError):
        CompleteUploadInput(expected_revision=revision)


def test_completion_and_cancellation():
    assert CompleteUploadInput(expected_revision=1).expected_revision == 1
    assert CancelUploadInput(expected_revision=1, reason="다른 자료를 올리겠습니다.").reason
    with pytest.raises(ValidationError):
        CancelUploadInput(expected_revision=1, reason="   ")


def test_models_do_not_grant_upload_or_accounting_authorization():
    item = UploadFileInput(**file_data(filename="unknown.xyz"))
    assert "approved" not in item.model_dump()
    assert "content_verified" not in item.model_dump()
    with pytest.raises(ValidationError):
        item.size_bytes = 500
