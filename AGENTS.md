# price: AI 세무사
Project and worker name are `price`; this is NOT a pricing/comparison product.
- Independent repository. Never write to CORP-OS or another workspace.
- Read docs/DEVELOPMENT_PLAN_KO.md and docs/IMPLEMENTATION_STATUS_KO.md before work.
- Read docs/OWNER_UPLOAD_FIRST_REQUIREMENTS_KO.md: the owner's explicit goal is upload-only accounting/tax automation for a non-expert. This corrects earlier UX/priorities, not the integrity/security boundaries below.
- The owner uploads bank statements, tax invoices and card records, answers only factual questions, and sees plain-language results. Never make the owner choose accounts/tax treatment or switch into a reviewer account to self-approve. Expert workbench is not the primary owner experience.
- Tests: `python -m pytest -q`; API: `python -m price serve`; worker: `python -m price.worker --once`.
- All production filing/payment paths are fail-closed. Simulated receipts always carry sandbox labels.
- No claimed live LLM, legal validation, official returns, or remote deployment without actual evidence.
- Money is strict integer KRW; tax math uses Decimal. Never accept LLM-calculated totals as authoritative.
- Company scope, authenticated actor, independent approval, version/hash checks, and audit are mandatory.
- Preserve original evidence and posted journals; correct by a new reversal or new revision before posting.
- Never include real certificates, credentials, personal data, or generated session DBs in the deliverable.
- Do not commit, push, install a service, or expose a public port without an explicit request.
- Product acceptance requires a supported real-format upload bundle to proceed without manual transaction entry or accounting expertise. Stored files, queued jobs, passing technical tests, and hidden unresolved reviews are not completed accounting/tax work.
