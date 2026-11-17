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

## Chat
The **Chat** view (top of the PatternVault side bar, or Workbench → Chat, or *PatternVault: Open Chat*) is the conversational front door to everything else.

* **Questions** are answered by your local model from the patterns PatternVault retrieves for the question; the answer streams in and cites its sources as `#id` chips (click one to read the pattern).
  A `#id` that was not among the retrieved patterns is shown dashed/amber: the model cited something it was not given. *Use my patterns* turns retrieval off.
* **Without a model** (Ollama stopped, no model chosen, or the mode forbids it) the chat says why and shows the matching stored patterns instead — clearly labelled as not written by a model.
* **Ask About Selection** (editor right-click or Command Palette) drops the selected code into the chat box with a question; nothing is sent until you press Enter. Credential-looking files are refused,
  and at most 12 000 characters of a selection are included.
* **Slash commands**: `/search`, `/context`, `/generate`, `/task`, `/recreate`, `/status`, `/new`, `/help` (see the README table). `/generate` only proposes. `/task` and `/recreate` start an orchestrated task:
  you confirm first, and it runs under your permission level with a recovery point, tests, correction and rollback exactly as in Code Generation.
* **History**: conversations are stored in your PatternVault data folder, with secrets redacted *before* storage. Use the header's selector to reopen one, **New chat** to start fresh, **Delete** to remove one
  (**Settings → Delete all chats…** removes every conversation). Only the last 20 messages of a conversation are sent to the model as context.
* **Cloud**: chat only uses the local model unless the mode is *hybrid* and you have consented to a cloud endpoint (see Permission levels / Privacy). The extension UI does not send cloud keys, so cloud answers are an API-only feature today.

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
