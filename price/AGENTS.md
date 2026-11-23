# price package

## OVERVIEW
Importable package `price` for the AI tax agent. 72 Python modules, 9706 lines, all flat in this directory (no subpackages). Docstrings are the behavior contract. Public names below were counted with `ast` on 2026-11-21; centrality was not measured.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Process entry | `cli.py` `main`, `__main__.py` | Console script `price = price.cli:main` |
| HTTP | `api.py` `create_app` | Mounts `static/`. Routes registered from `*_routes.py` |
| Owner upload HTTP | `upload_routes.py`, `uploads.py` | Resumable, company-scoped. Models in `upload_models.py` do no file IO |
| Durable worker | `worker.py` `main` | Proposals only. No post, approve, or submit |
| Policy executor | `automatic_worker.py`, `book_schedule_worker.py` | Separate from `worker.py`. No model or bank tools |
| Workflow state | `service.py` `Service` | Company-scoped, transactional |
| Persistence | `db.py` | Append-only audit. `sql_types.py` checks sqlite values |
| Auth | `auth.py` | `password_hash`, `token_hash`, `Actor` |
| Owner views | `owner.py` | Factual onboarding, not tax approval |
| Proposals | `ai.py` `Advisor` | Ollama adapter, no execution tools |
| Ledger books | `*_books.py`, `book_schedules.py`, `finance_ops.py` | Source-backed. Review is separate from posting |
| Tax papers | `vat_returns.py`, `vat_rules_kr.py`, `tax_workpapers.py`, `withholding_rules.py` | Components and workpapers, not a filed return |
| Filing simulator | `filing.py`, `submission_gateway.py` | Local simulator and labelled sandbox receipts |
| Ingest | `ingestion.py`, `ingest_safety.py`, `extractions.py` | Non-executing inspection. Replay keeps originals |
| Source readers | `source_*.py`, `native_documents.py` | Tables, OOXML stored values, inert HTML, text-layer PDF, KEC XML |
| Notion drafts | `notion_finance_ingest.py`, `notion_finance_map.py` | Drafts only. Does not journal or file |
| Demo | `demo.py` `seed_demo` | Synthetic. Refuses an initialized database |
| Release gate | `release_readiness.py`, `release_sources.py` | Source presence is not acceptance |

## CONVENTIONS
- Money stays integer KRW; tax math uses Decimal. `calculations.py` is helpers, not a full Korean tax pack.
- `accounts.py` is the internal book vocabulary, not a statutory classification.
- Route modules only register handlers. `operations_routes.py` is the integrated workpaper API.
- Casework (`casework.py`, `casework_routes.py`) asks facts. It does not approve tax treatment.

## ANTI-PATTERNS
- Do not treat `Advisor`, `worker.py`, or `automation.py` as allowed to post, pay, or file.
- `source_workbooks.py` and `source_html.py` must not evaluate formulas or execute markup.
- `native_documents.py` is text-layer only, not OCR success.
- `filing.py` is an explicitly labelled local simulator.
