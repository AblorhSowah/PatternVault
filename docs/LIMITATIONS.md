# Known limitations (kept honest)

**Verification status**
* **Real-model generation is not verified.** Ollama's documented HTTP API is implemented and tested against *protocol doubles*; no real Ollama/model was
  run (the sandbox denied executing a downloaded binary). An opt-in live test exists (`PV_OLLAMA_URL=… pytest tests/e2e/test_live_ollama.py -m live`).
  Local-model *quality* (and small models' ability to follow the `<<<FILE>>>` format) is therefore unmeasured; the parser has a markdown fallback and a repair loop.
* Continue's session format is implemented from its documentation, not verified against a real installation.
* ChromaDB as the vector backend is implemented behind the interface but was **not exercised** (optional extra not installed).
* Tested on macOS/arm64, Python 3.13, VS Code 1.129 only. Windows/Linux, Python 3.11/3.12 are untested (paths and process-group handling are POSIX-first).

**Chat**
* Like generation, chat answers have only been exercised against Ollama *protocol doubles*; answer quality, citation accuracy and how small models handle the prompt are unmeasured.
* Cloud chat needs a per-request key header that the extension UI does not send (the same is true of cloud generation), so it is reachable through the API only.
* Slash-command results (search cards, task panels) live in the open view, not in the saved history; an answer being written is lost if the backend restarts (what was already saved is kept).
* Answers render a small Markdown subset (paragraphs, lists, headings, code, bold, citations); tables, images and links are shown as plain text on purpose.
* Conversations are kept until you delete them; there is no automatic retention limit.

**Provider capture**
* GitHub Copilot and Cursor expose no supported way to read chats; they are workspace-observation/manual-capture only. Claude Code capture depends on a
  transcript format that is not a public contract.
* Hidden reasoning (`thinking` blocks) is deliberately never captured.

**Intelligence**
* Pattern extraction and categorisation are deterministic heuristics (no LLM): titles come from the prompt's first sentence; unusual stacks may be mis-categorised.
* The default embedder is *lexical* (feature hashing); semantic recall improves with an Ollama embedding model.
* Quality analysis (duplicates/conflicts/outdated) is advisory and conservative.

**Testing agents**
* API probing targets FastAPI apps; the database agent checks SQLite files and PatternVault-style migrations; the extension agent does not run the VS Code
  extension-host tests (use `scripts/check.sh --host`). Dependency audits need network access and are `skipped` offline.
* Performance budgets are generous and machine-dependent.

**Product**
* Installing the extension requires **Set Up Backend** once (Python 3.11+, network access to PyPI for dependencies). After that it is fully offline.
* Single-user, single-machine design: SQLite single writer, no sync, no multi-user permissions.
* No Language Server (ADR-009).
