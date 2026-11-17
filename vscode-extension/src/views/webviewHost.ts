import { writeFile } from 'node:fs/promises';
import * as vscode from 'vscode';
import type { BackendManager } from '../services/backendManager';
import { handleRpc, RpcMessage } from '../services/webviewRpc';

/**
 * Wires one webview (the Workbench panel or the chat sidebar) to the backend. Every webview is treated as untrusted:
 * it only gets what `handleRpc` allows, and live backend events are forwarded to it.
 * `onReady` fires when the webview reports that its scripts have loaded.
 */
export function attachWebview(web: vscode.Webview, backend: BackendManager, onReady?: () => void): vscode.Disposable {
  const events = backend.onDidReceiveEvent((event) => void web.postMessage({ type: 'event', event }));
  const messages = web.onDidReceiveMessage(async (m: RpcMessage | { type: 'ready' }) => {
    if (!m) return;
    if (m.type === 'ready') {
      onReady?.();
      return;
    }
    const res = await handleRpc(m as RpcMessage, {
      getClient: () => backend.client,
      confirm: async (message) => (await vscode.window.showWarningMessage(message, { modal: true }, 'Continue')) === 'Continue',
      saveJson: async (name, data) => {
        const target = await vscode.window.showSaveDialog({ defaultUri: vscode.Uri.file(name), filters: { JSON: ['json'] } });
        if (!target) return false;
        await writeFile(target.fsPath, JSON.stringify(data, null, 2), 'utf8');
        return true;
      },
      runCommand: async (command) => {
        await vscode.commands.executeCommand(command);
      },
      copyText: async (text) => {
        await vscode.env.clipboard.writeText(text);
      },
      openText: async (text, language) => {
        const doc = await vscode.workspace.openTextDocument({ content: text, language });
        await vscode.window.showTextDocument(doc, { preview: false });
      },
    });
    if (res) void web.postMessage(res);
  });
  return vscode.Disposable.from(events, messages);
}
