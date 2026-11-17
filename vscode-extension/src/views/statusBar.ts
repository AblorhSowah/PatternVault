import * as vscode from 'vscode';
import { statusBarModel } from '../services/statusModel';
import type { BackendManager } from '../services/backendManager';
import type { Dashboard } from '../services/backendClient';

export class StatusBar implements vscode.Disposable {
  private readonly item: vscode.StatusBarItem;
  dashboard: Dashboard | undefined;

  constructor(private readonly backend: BackendManager) {
    this.item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
    this.item.name = 'PatternVault';
    this.render();
    this.item.show();
  }

  render(): void {
    const m = statusBarModel(this.backend.status, this.dashboard, this.backend.lastError);
    this.item.text = m.text;
    this.item.tooltip = m.tooltip;
    this.item.command = m.command;
    this.item.backgroundColor = m.warning ? new vscode.ThemeColor('statusBarItem.warningBackground') : undefined;
  }

  dispose(): void {
    this.item.dispose();
  }
}
