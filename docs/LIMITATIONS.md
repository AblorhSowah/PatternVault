# Known limitations (kept honest)

**Verification status**
* **Real-model generation was verified only narrowly (2026-11-17).** One model — `qwen2.5-coder:7b` on Ollama 0.40.16, macOS arm64, 16 GB, about 15 tokens/s — was run for real:
  the opt-in live test passes and 6 further identical requests each returned two files that passed their own tests (16–31 s each). The *first* real run had failed
  (see the changelog), so the code is now correct for what that model did, which says little about other models. **Not verified with a real model:** other or smaller models
  (e.g. 1.5B), larger or multi-file tasks, *adapt* mode with retrieved patterns (the live request had no stored knowledge), the `/task` agent loop, languages other than Python.
  Expect a small model to ignore the requested file format or to ramble; the parser, the answer cap and the early stop exist because the 7B model did both.
* Continue's session format is implemented from its documentation, not verified against a real installation.
* ChromaDB as the vector backend is implemented behind the interface but was **not exercised** (optional extra not installed).
* Tested on macOS/arm64, Python 3.13, VS Code 1.129 only. Windows/Linux, Python 3.11/3.12 are untested (paths and process-group handling are POSIX-first).

**Chat**
* Chat was tried with a real model (the same 7B): a greeting (4 s), a grounded question about a stored pattern (46 s for a 700-token answer) and a deliberately capped answer. That is a handful of samples, not a quality measurement.
  The model sometimes writes "Pattern #1" in prose instead of the `[#1]` the UI turns into a link; the source chips under every answer are always taken from the actual retrieval, so they stay correct.
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
