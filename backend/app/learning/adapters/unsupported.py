"""Providers that expose no supported capture channel.

They are registered so the UI can show them honestly. For these, PatternVault uses *workspace observation*
(file changes between a user-started and user-ended capture window) and *manual capture* (user supplies the
prompt/response). No scraping of private databases or extension internals is attempted.
"""

from __future__ import annotations

from app.learning.adapters.base import Capabilities, ProviderAdapter


class _NoCapture(ProviderAdapter):
    def available(self) -> bool:
        return False  # data source cannot be read; installation is reported by the VS Code extension


class CopilotAdapter(_NoCapture):
    key = "github-copilot"
    display_name = "GitHub Copilot"
    vscode_extension_ids = ("github.copilot", "github.copilot-chat")
    capabilities = Capabilities(
        mechanism="workspace observation + manual capture",
        notes="Copilot Chat has no public API or documented local log for reading conversations, so chats are "
        "not captured automatically. Use a capture window (workspace diff) or paste the exchange manually.",
    )


class CursorAdapter(_NoCapture):
    key = "cursor"
    display_name = "Cursor"
    vscode_extension_ids = ()
    capabilities = Capabilities(
        mechanism="workspace observation + manual capture",
        notes="Cursor keeps chats in an undocumented internal database; PatternVault does not read it. "
        "Use a capture window (workspace diff) or paste the exchange manually.",
    )


class ManualAdapter(_NoCapture):
    key = "manual"
    display_name = "Manual / workspace capture"
    kind = "manual"
    capabilities = Capabilities(
        captures_prompts=True,
        captures_responses=True,
        captures_file_edits=True,
        mechanism="user-entered prompts/responses + observed workspace changes",
        verified_against_real_install=True,
        notes="Works with any assistant: start a capture window, work, end it.",
    )

    def available(self) -> bool:
        return True
