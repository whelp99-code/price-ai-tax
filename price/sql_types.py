"""Validate opaque sqlite driver values at the native row boundary."""
from __future__ import annotations

import sqlite3
from typing import TypeVar, cast

Value = TypeVar("Value")


def read_row(cursor: sqlite3.Cursor) -> sqlite3.Row | None:
    result = cast(object, cursor.fetchone())
    if result is None:
        return None
    if not isinstance(result, sqlite3.Row):
        raise TypeError("Expected the native sqlite.Row factory")
    return result


def row_value(row: sqlite3.Row, key: str | int, expected: type[Value]) -> Value:
    value = cast(object, row[key])
    if not isinstance(value, expected):
        raise TypeError(f"Unexpected SQLite value type for {key}")
    return value
