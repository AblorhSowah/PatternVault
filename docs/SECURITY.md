# Security model and review

## Assets and threats

| Asset | Threat | Control (and the test that pins it) |
|---|---|---|
| Source code & secrets in the user's projects | secrets copied into the knowledge base, embeddings, prompts, audit log, backups | Secrets are redacted **before** persistence/embedding/prompting and again at the pattern-store boundary and on export; `.env`, private keys, credential files, binaries are never read (`tests/security/test_knowledge_privacy.py`, `tests/unit/test_secrets.py`, `test_detector_precision.py`) |
| Local backend API | other local processes / web pages calling it | loopback bind, per-launch random bearer token (constant-time compare), Host-header allow-list, no OpenAPI/docs exposure, WebSocket first-message auth (`tests/security/test_api_security.py`) |
| API keys | leakage to disk/logs | VS Code SecretStorage only; config rejects secret-shaped fields; keys supplied per request are memory-only (`test_cloud_backend_consent_key_and_no_persistence`, `test_cloud_key_with_a_task_is_never_persisted`) |
| User files | destructive or out-of-scope agent actions | central permission gate (4 modes), destructive-command denylist in every mode, writes outside the project/`.git` denied, sensitive files always need approval, **recovery point before every change**, automatic rollback after failed corrections (`tests/unit/test_permissions.py`, `tests/integration/test_execution_engine.py`, `test_orchestrator.py`) |
| The agents themselves | prompt injection via captured transcripts, retrieved patterns, repository files, model output | all such text is *data*: scanned & flagged, wrapped in randomised `UNTRUSTED_DATA` blocks, never executed; model output paths are validated and traversal/`.git` paths dropped at parse time; embedded "instructions" in generated code are review findings (`test_prompt_injection_in_reference_pattern_is_contained`, `test_hostile_output_is_blocked`, `tests/unit/test_untrusted_and_blobs.py`) |
| The Workbench webview | XSS from untrusted pattern/session text → calls to the backend | DOM built with `textContent` only (static scan forbids `innerHTML`/`eval`), strict nonce CSP, **host-side API allow-list**, host-enforced confirmation for deletes/escalation/task start/approvals/restore, token never enters the webview (`vscode-extension/test/unit/webview.test.ts`, `tests/e2e/test_workbench.py::test_hostile_content_renders_inert`) |
| Chat conversations | secrets typed or pasted into chat reaching the model, the database or the audit log; hostile text in an answer; the chat escalating to file changes | the message is redacted before it reaches the model **and** before it is stored; replies are redacted before storage; the audit log records metadata only (ids, backend, model, duration — never message text); answers render as text nodes only (no HTML, no links); the chat's allow-listed routes contain no write path — `/task` goes through `POST /tasks` with host-enforced confirmation; cloud only in hybrid mode with consent and a per-request, memory-only key; *Ask About Selection* refuses credential-looking files; retrieved patterns are wrapped as `UNTRUSTED_DATA` (`tests/integration/test_chat.py`, `tests/e2e/test_chat_ui.py::test_hostile_answers_are_inert`, `vscode-extension/test/unit/chat.test.ts`) |
| Knowledge from other people (import) | poisoned/forged "verified" patterns, path traversal on replay | imports re-enter as `observed` (evidence is not transferable), re-redacted, pattern paths validated at replay (`test_export_import_roundtrip_demotes_status`, `test_replay_blocks_malicious_imported_pattern`) |
| Backups | disclosure, tampering, malicious archives | scrypt + Fernet encryption (authenticated), 0600 files, archive members whitelisted before extraction, schema/integrity validated on a staging copy, safety copy before restore, audit trail never rewound (`tests/integration/test_backup_restore.py`) |
| Audit trail | silent edits/deletions | hash chain (`verify_chain`), details redacted; restore carries the live trail over |
| Children spawned by agents | inheriting secrets/PatternVault's token | sanitised environment (secret-named variables and `PATTERNVAULT_*` dropped), process-group kill on timeout/cancel, output redacted and capped |

## Verification gate (integrity of the *knowledge*)

A task is `verified`, and a pattern is promoted to `tested`/`verified`, **only** through a persisted passing testing session that
executed ≥ 1 real test. "Generation finished", model confidence, user clicks or an assistant's own transcript claiming passing tests
never grant those states. Skipped suites are `skipped`, never `passed`. See `knowledge/lifecycle.py`, `Orchestrator.evaluate_gate`.

## Review performed (2026-11-17)

* `bandit -r backend/app` — clean at the configured level (B101/B404/B603 are intentionally skipped: subprocess use is a core feature,
  argv-list only, never `shell=True`, and every agent-driven command passes `security/permissions.py` first).
* Security Testing Agent run against this repository (excluding test fixtures that intentionally contain insecure code): see
  `PROJECT_PROGRESS.md` for the final numbers. Earlier runs surfaced six false positives in the detectors themselves, fixed and pinned.
* Dependency audit: `npm audit --omit=dev` and `pip-audit` — results in `PROJECT_PROGRESS.md`.
* A Trojan-Source scan (invisible/bidirectional Unicode in first-party source) runs as a test.
* Defects found *by the test suites* that were security-relevant: placeholder filter swallowing real secrets; pattern store trusting callers
  to redact; backup restore rewinding the audit log; GC deleting live-session baselines; skip-propagation letting `verify` run on unwritten code.

## Known residual risks

* The audit chain is tamper-*evident*, not tamper-proof: an attacker with write access to the DB can rewrite the whole chain or truncate its tail.
* Regex-based secret detection has false negatives (novel token formats, secrets split across lines/concatenation). Treat redaction as defence in depth,
  not a guarantee; do not store credentials in source.
* Recovery points hold **raw** copies of files an agent is about to change (needed for exact rollback). They are private (0700/0600) but not encrypted.
* Allow-listed verification commands (pytest, npm test…) execute the project's own code with the user's privileges, by design.
* Local models are untrusted components; PatternVault limits what their output can do (path validation, review, permissions) but cannot judge its correctness
  beyond the tests that exist.
