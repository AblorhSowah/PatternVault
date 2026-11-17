# User guide

## The three modes
| Mode | What PatternVault does | Network |
|---|---|---|
| **Learning** | observes your AI assistants' local logs / your capture windows and learns patterns | none required |
| **Offline** | uses only the local knowledge base and the local model | never |
| **Hybrid** | local model and (with consent) a cloud endpoint; compare outputs; automatic fallback | only to the endpoint you consented to |

Switch in the status bar, the Dashboard or Settings. With *automatic fallback* on, an unavailable preferred backend falls back to what is allowed (and the switch is audited).

## Teaching PatternVault
* **Capture window:** *PatternVault: Start Capture Window* → work with any assistant → *End Capture Window and Learn*. File changes are diffed; secrets are redacted.
* **Import assistant sessions:** *Import AI Assistant Sessions* reads supported local logs (Claude Code; Continue unverified). Copilot/Cursor cannot be read — use capture windows.
* New patterns start as **observed**. Review them in *AI Learning Centre* → approve or reject.

## Trust levels
`observed → generated → approved → tested → verified` (+ `deprecated`). `tested`/`verified` require a **passing test run** — approvals and clicks alone cannot grant them.

## Generating and recreating code
*Code Generation* workspace: describe the change, pick the project, optionally the knowledge to use.
* **Analyze context** shows how the request was understood and what is missing.
* **Generate / Plan recreation** only *propose* changes (diffs, automatic checks).
* **Run end-to-end task** applies them under your permission level: recovery point → review → apply → tests → (on failure) debugging agent proposes a fix → independent retest →
  rollback if still failing → learn only if verified.
* **Recreate and verify** replays a stored implementation without needing a model.

## Permission levels
`read_only` (proposals only) · `assisted` (approve every write/command) · `approved_execution` (approve the plan once) · `restricted_autonomous` (safe in-project edits and allow-listed test commands run on their own).
Destructive commands, writes outside the project, `.git` writes are **always** blocked; sensitive files (`package.json`, CI, `.env`, scripts…) always need approval. Approvals appear in the Agent Control Centre and inline in tasks.

## Safety nets
* Every change is preceded by a **recovery point**; *Agent Control Centre → Recovery points → Restore* undoes it.
* *Settings → Backup and restore* — create encrypted backups (a lost passphrase cannot be recovered); a safety copy is taken before any restore.
* *Settings → Audit log* shows what agents did; *Verify audit chain* checks it for tampering.

## Testing Centre
Ten testing agents (unit, integration, e2e, API, database, extension, security, performance, regression, code quality). Results show expected vs actual, error details, any
correction applied and the retest outcome. Anything that could not run is **skipped**, never "passed".

## Privacy
Local-first; nothing is sent anywhere without explicit consent; `.env`/keys/credentials are never read; secrets are redacted before storage; add your own exclusions in Settings.
API keys go to VS Code SecretStorage only (*Store API key…*).
