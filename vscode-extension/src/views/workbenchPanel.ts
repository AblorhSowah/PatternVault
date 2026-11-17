import { randomBytes } from 'node:crypto';
import { writeFile } from 'node:fs/promises';
import * as vscode from 'vscode';
import type { BackendManager } from '../services/backendManager';
import { handleRpc, RpcMessage } from '../services/webviewRpc';
import { makeNonce, renderWorkbenchHtml } from './webviewHtml';

export class WorkbenchPanel implements vscode.Disposable {
  private panel: vscode.WebviewPanel | undefined;
  private readonly subs: vscode.Disposable[] = [];

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly backend: BackendManager,
  ) {
    this.subs.push(this.backend.onDidReceiveEvent((event) => void this.panel?.webview.postMessage({ type: 'event', event })));
  }

  show(): void {
    if (this.panel) {
      this.panel.reveal();
      return;
    }
    const media = vscode.Uri.joinPath(this.context.extensionUri, 'media');
    const panel = vscode.window.createWebviewPanel('patternvault.workbench', 'PatternVault', vscode.ViewColumn.Active, {
      enableScripts: true,
      retainContextWhenHidden: true,
      localResourceRoots: [media],
    });
    this.panel = panel;
    const web = panel.webview;
    web.html = renderWorkbenchHtml({
      cspSource: web.cspSource,
      nonce: makeNonce((n) => randomBytes(n)),
      scriptUri: web.asWebviewUri(vscode.Uri.joinPath(media, 'webview', 'workbench.js')).toString(),
      styleUri: web.asWebviewUri(vscode.Uri.joinPath(media, 'webview', 'workbench.css')).toString(),
    });
    web.onDidReceiveMessage(async (m: RpcMessage | { type: 'ready' }) => {
      if (!m || m.type === 'ready') return;
      const res = await handleRpc(m as RpcMessage, {
        getClient: () => this.backend.client,
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
      });
      if (res) void web.postMessage(res);
    });
    panel.onDidDispose(() => (this.panel = undefined));
  }

  dispose(): void {
    this.panel?.dispose();
    for (const s of this.subs) s.dispose();
  }
}
