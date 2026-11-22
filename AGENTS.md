# PROJECT KNOWLEDGE BASE

**Generated:** 2026-11-21T13:20:17Z
**Commit:** ae5c4a6
**Branch:** fix/price-typecheck-exit1-11

## OVERVIEW
AI tax agent (`price-tax-agent` 0.2.1), product name AI 세무사. Project and worker name are `price`; this is NOT a pricing/comparison product. Python >=3.11; this checkout's README names system Python 3.14.4. FastAPI, SQLite, Korean owner UI.

## STRUCTURE
```
price/
├── price/          # package: CLI, API, worker, static/*, schema.sql
├── tests/          # pytest namespace, no __init__.py (score 9)
├── scripts/        # flat CLIs, not an installable package (score 7)
├── docs/           # Korean product docs + generated openapi.json
└── pyproject.toml  # console script price = price.cli:main
```

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Before work | docs/DEVELOPMENT_PLAN_KO.md, docs/IMPLEMENTATION_STATUS_KO.md, docs/OWNER_UPLOAD_FIRST_REQUIREMENTS_KO.md | Upload-first goal. Does not relax integrity rules |
| Package map | price/AGENTS.md | 72 flat modules, 9706 lines. Entries: cli.main, api.create_app, worker.main, service.Service |
| API mount | price/api.py | Serves price/static at /static. `/` is owner.html when PRICE_OWNER_UI default "1"; `/expert` is index.html; `/cashflow` and `/bookkeeping` are separate pages |
| Owner UI | price/static/owner.html | Load order: owner.js, discrepancies.js, casework.js, book_results.js. Later files call element, button, prefix, request, post as bare globals |
| Expert UI | price/static/app.js | type=module on the expert page only |
| Worker manifest | worker.json | command `python3 -m price.worker`. registrationStatus manifest_only_not_registered_in_Orca |
| Runtime | scripts/runtime.py | State `.data/runtime.json`. Fixed port 4381. Loopback. Stops only processes it recorded |
| Company DB | docs/ops/COMPANY_DB.md | PRICE_COMPANY_DB default `/home/jm/.local/share/price/company.sqlite3`. Backups keep 14, mode 0600 |
| Tests | tests/conftest.py | World, pristine, w, synthetic PASSWORD. Cross-test imports include leading-underscore helpers |
| One-shot rewriters | scripts/ | apply_v01_integration.py, fix_v01_late_facts.py, fix_v01_ownership.py, integrate_v01_readers.py, update_expert_browser_route.py, build_extended_owner_e2e.py, build_reader_replay_e2e.py. Match one exact source block, then write. Not libraries |
| OpenAPI | docs/openapi.json | Generated from current code (2635 lines), not a hand-written contract |
| Releases | docs/releases/ | Release notes and versions.json |

## CODE MAP
Centrality unmeasured. Package public names are in `price/AGENTS.md`. Digest recorded loaded symbols for tests and scripts.

| Symbol | Type | Location | Role |
|--------|------|----------|------|
| price.cli:main | console script | pyproject.toml | `price` entry |
| install | function | scripts/install_standalone.py | Tests load it. Raises NOT_STANDALONE for a CORP-OS destination |
| identity, save, health, ROOT, DATA, STATE, PORT | names | scripts/runtime.py | Tests monkeypatch these |
| manifest | function | scripts/verify_engineering_snapshot.py | Sibling script import |
| read_hometax_purchases | function | scripts/notion_finance_s2.py | Ingest script load |

## CONVENTIONS
- Independent repository. Never write to CORP-OS or another workspace.
- Read docs/DEVELOPMENT_PLAN_KO.md and docs/IMPLEMENTATION_STATUS_KO.md before work.
- Read docs/OWNER_UPLOAD_FIRST_REQUIREMENTS_KO.md: the owner's explicit goal is upload-only accounting/tax automation for a non-expert. This corrects earlier UX/priorities, not the integrity/security boundaries below.
- The owner uploads bank statements, tax invoices and card records, answers only factual questions, and sees plain-language results.
- scripts/ reuse is `sys.path.insert` or `importlib.util.spec_from_file_location`, never `import scripts`.
- Money is strict integer KRW; tax math uses Decimal. Never accept LLM-calculated totals as authoritative.
- Company scope, authenticated actor, independent approval, version/hash checks, and audit are mandatory.
- Preserve original evidence and posted journals; correct by a new reversal or new revision before posting.
- Bind is localhost only: `127.0.0.1:4381`. Demo DB `.data/demo.sqlite3` is gitignored. Demo password is synthetic-only.
- Seed scripts (`seed_bookkeeping_e2e.py`, `seed_cashflow_browser.py`) create a new sqlite file. They never open the demo or production DB.
- `fetch` uses `credentials: 'same-origin'`. UI language is Korean. No frontend framework.
- Pins in both `pyproject.toml` and `requirements.txt`: fastapi 0.141.1, uvicorn 0.52.4, pydantic 2.13.5, httpx 0.28.1. Tests pin pytest 8.4.2. Optional reader lock pins `xlrd==2.0.2` with a sha256. No executable document content is evaluated.
- Ubuntu has no pip. Wheel build used setuptools `build_wheel`, not a pip install test.
- Package-data: `price = ["static/*", "schema.sql"]`.

## ANTI-PATTERNS (THIS PROJECT)
- Never make the owner choose accounts/tax treatment or switch into a reviewer account to self-approve. Expert workbench is not the primary owner experience.
- All production filing/payment paths are fail-closed. Simulated receipts always carry sandbox labels.
- No claimed live LLM, legal validation, official returns, or remote deployment without actual evidence.
- Never include real certificates, credentials, personal data, or generated session DBs in the deliverable.
- Do not commit, push, install a service, or expose a public port without an explicit request.
- Product acceptance requires a supported real-format upload bundle to proceed without manual transaction entry or accounting expertise. Stored files, queued jobs, passing technical tests, and hidden unresolved reviews are not completed accounting/tax work.
- A pytest pass is not browser acceptance and is not a filed return.
- `notion_finance_ingest.py` refuses `.data/demo.sqlite3`. `install_standalone.py` raises `NOT_STANDALONE` for a CORP-OS destination and never overwrites an existing destination.
- `integrate_v01_readers.py`: do not invent table readers for arbitrary PDFs, scans, encrypted documents, or old XLS.
- `worker.json` prohibited capabilities: approve, post_journal, live_submit, payment, shell. No systemd autostart.
- Browser acceptance is `/usr/bin/google-chrome` headless over the DevTools WebSocket. Node builtins only. No Playwright, no npm.

## UNIQUE STYLES
- Owner scripts are classic globals. `owner.html` load order is the contract. `app.js` is the only module.
- One-shot rewriters match one exact source block, then write `price/` or another script.
- Tests are a namespace package: 60 modules, 614 defs, 14 sibling-imported helpers. No `tests/__init__.py`.

## COMMANDS
```bash
cd /home/jm/orca/projects/price
python -m pytest -q
python -m price serve
python -m price.worker --once
python3 scripts/runtime.py status|start|stop
python3 -m price --db .data/demo.sqlite3 status
python3 -m price --db .data/demo.sqlite3 serve --host 127.0.0.1 --port 4381
python3 -m price.worker --db .data/demo.sqlite3 --once
python3 -m pytest -q
python3 scripts/e2e_http.py
node scripts/e2e_browser.mjs
node --check price/static/app.js
python3 -m compileall -q price
python3 scripts/company_db_backup.py
```
No Makefile and no `package.json`. Pytest: `testpaths = ["tests"]`, `addopts = "-ra"`.

## NOTES
- Package internals: `price/AGENTS.md`. Counted 2026-11-21 with ast: 72 modules, 9706 lines, no subpackages. Scanner chunks had covered root, tests, scripts, docs, and price/static only; the package file closes that gap.
- Child files: `price/AGENTS.md`; `tests/AGENTS.md` (score 9). Skipped: `scripts/` 7, `price/static/` 4, `docs/` 0, `docs/ops/` 0, `docs/releases/` 0, `docs/tasks/` 0. This root covers them.
- Company DB is not the demo DB.
