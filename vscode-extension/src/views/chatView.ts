import { randomBytes } from 'node:crypto';
import * as vscode from 'vscode';
import type { BackendManager } from '../services/backendManager';
import { makeNonce, renderWorkbenchHtml } from './webviewHtml';
import { attachWebview } from './webviewHost';

/** The chat, docked in the PatternVault side bar. The same webview code also appears as a Workbench section. */
export class ChatViewProvider implements vscode.WebviewViewProvider, vscode.Disposable {
  static readonly viewType = 'patternvault.chat';
  private view: vscode.WebviewView | undefined;
  private ready = false;
  private pendingPrefill: string | undefined;
  private wiring: vscode.Disposable | undefined;

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly backend: BackendManager,
  ) {}

  resolveWebviewView(view: vscode.WebviewView): void {
    this.wiring?.dispose();
    this.view = view;
    this.ready = false;
    const media = vscode.Uri.joinPath(this.context.extensionUri, 'media');
    const web = view.webview;
    web.options = { enableScripts: true, localResourceRoots: [media] };
    web.html = renderWorkbenchHtml({
      cspSource: web.cspSource,
      nonce: makeNonce((n) => randomBytes(n)),
      scriptUri: web.asWebviewUri(vscode.Uri.joinPath(media, 'webview', 'chat.js')).toString(),
      styleUri: web.asWebviewUri(vscode.Uri.joinPath(media, 'webview', 'workbench.css')).toString(),
    });
    this.wiring = attachWebview(web, this.backend, () => {
      this.ready = true;
      this.flush();
    });
    view.onDidDispose(() => {
      this.wiring?.dispose();
      this.wiring = undefined;
      if (this.view === view) this.view = undefined;
      this.ready = false;
    });
  }

  /** Reveal the chat. */
  async focus(): Promise<void> {
    await vscode.commands.executeCommand(`${ChatViewProvider.viewType}.focus`);
  }

  /** Open the chat and put text in its input box. It is never sent automatically: the user reviews it first. */
  async prefill(text: string): Promise<void> {
    this.pendingPrefill = text;
    await this.focus();
    this.flush();
  }

  private flush(): void {
    if (!this.view || !this.ready || this.pendingPrefill === undefined) return;
    void this.view.webview.postMessage({ type: 'prefill', text: this.pendingPrefill });
    this.pendingPrefill = undefined;
  }

  dispose(): void {
    this.wiring?.dispose();
  }
}
