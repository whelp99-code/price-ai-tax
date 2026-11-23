# tests

## OVERVIEW
Pytest suite for the price tax agent (score 9: 60 modules, 614 defs, cross-test exports). Namespace package: no `tests/__init__.py`.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Shared world | `conftest.py` | `World`, fixtures `pristine` (session) and `w` |
| Owner upload API | `test_owner_upload_api.py` | helpers `send`, `start` imported by siblings |
| Payable coverage | `test_payable_coverage.py` | largest file (338 lines); `INVOICE`, `PO`, `coverage` |
| Casework | `test_casework.py` | `answer`, `invoice`, `setup_sources` |
| Event settlements | `test_event_settlements.py` | `data_for` |
| Notion finance map | `test_notion_finance_ingest.py` | `_apply`, `_row` (leading underscore, still imported) |
| Notion script text | `test_notion_finance_map.py` | reads `scripts/notion_finance_s2.py` as text, does not import it |
| Packaging / runtime | `test_packaging_runtime.py`, `test_runtime_recovery.py` | load `scripts/*.py` via `importlib.util.spec_from_file_location` |
| Filing / VAT / withholding | `test_vat_returns.py`, `test_tax_workpapers.py`, `test_withholding_rules.py`, `test_submission_gateway.py` | fail-closed paths; do not treat a green run as a filed return |
| Books | `test_*_books.py`, `test_book_schedules.py` | card, compensation, inventory, opening, payment |
| Sources | `test_source_*.py`, `test_pdf_table_sources.py`, `test_native_*.py` | ingestion, workbooks, XML, HTML |

## CONVENTIONS
- Pytest lives in repo `pyproject.toml`: `testpaths = ["tests"]`, `addopts = "-ra"`, extra `pytest==8.4.2`. No `pytest.ini`.
- Run `python3 -m pytest -q` from the repo root. Interpreter floor is `>=3.11`; this checkout's checks use 3.14.4.
- Reuse goes through `from tests.<module> import ...`, including leading-underscore names. Do not add `tests/__init__.py` or a helpers package for one caller.
- `PASSWORD = 'Test-Only-Price-2026!'` is synthetic. `World.tx` defaults are synthetic Korean counterparties and integer KRW (`net=1000`).
- `pristine` bootstraps company A plus reviewer/preparer/viewer, copies a backup, and `w` restores that backup into `tmp_path`. Tests do not open `.data/demo.sqlite3` or the company DB.
- Evidence bytes in `w` are the literal `SYNTHETIC ACCOUNTING EVIDENCE - NOT REAL TAX DATA`.
- 534 `test_*` functions. Nothing outside `tests/` imports `tests.*` (historical XML under `evidence/` is not an import site).

## ANTI-PATTERNS
- Do not point a test at the demo DB, `PRICE_COMPANY_DB`, or a live filing endpoint. `World` is a copied sqlite file.
- Do not import `price.notion_finance_ingest` when the assertion is about `scripts/notion_finance_s2.py`; the map test reads the script as text.
- A full pytest pass does not prove browser acceptance. HTTP and Chrome checks live in `scripts/e2e_http.py` and `scripts/e2e_*.mjs`.
