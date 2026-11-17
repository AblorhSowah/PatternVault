# PatternVault for VS Code

Local-first AI development intelligence: PatternVault observes AI-assisted work (using only supported, local sources),
learns *verified* patterns, and reuses them with a local model when cloud AI is unavailable.

**First run:** run **PatternVault: Set Up Backend** once. It creates a private Python environment (Python 3.11+ required)
and installs the bundled backend (network access to PyPI is needed once for dependencies). After that PatternVault works
fully offline. Open the **PatternVault** activity-bar view or run **PatternVault: Open Workbench**.

**Chat:** the **Chat** view at the top of the PatternVault side bar answers questions from your stored patterns (select code → right-click → *PatternVault: Ask About Selection*).
Type `/` for commands such as `/search`, `/generate` and `/task`.

Optional: install [Ollama](https://ollama.com) and pull a coding model to enable local generation.

Privacy: no data leaves your machine unless you explicitly consent to a cloud endpoint; secrets are redacted before
anything is stored; API keys live only in VS Code SecretStorage.
