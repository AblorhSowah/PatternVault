import { randomBytes } from 'node:crypto';
import * as vscode from 'vscode';
import type { BackendManager } from '../services/backendManager';
import { makeNonce, renderWorkbenchHtml } from './webviewHtml';
import { attachWebview } from './webviewHost';

export class WorkbenchPanel implements vscode.Disposable {
  private panel: vscode.WebviewPanel | undefined;

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly backend: BackendManager,
  ) {}

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
    const wiring = attachWebview(web, this.backend);
    panel.onDidDispose(() => {
      wiring.dispose();
      this.panel = undefined;
    });
  }

  dispose(): void {
    this.panel?.dispose();
  }
}
