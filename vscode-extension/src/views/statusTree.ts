import * as vscode from 'vscode';
import type { BackendManager } from '../services/backendManager';
import type { Dashboard } from '../services/backendClient';
import { modeLabel } from '../services/statusModel';

export class StatusTree implements vscode.TreeDataProvider<vscode.TreeItem> {
  private readonly emitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.emitter.event;
  dashboard: Dashboard | undefined;

  constructor(private readonly backend: BackendManager) {}

  refresh(): void {
    this.emitter.fire();
  }

  getTreeItem(el: vscode.TreeItem): vscode.TreeItem {
    return el;
  }

  getChildren(): vscode.TreeItem[] {
    const row = (label: string, description: string, icon: string, command?: string): vscode.TreeItem => {
      const item = new vscode.TreeItem(label);
      item.description = description;
      item.iconPath = new vscode.ThemeIcon(icon);
      if (command) item.command = { command, title: label };
      return item;
    };
    const status = this.backend.status;
    const open = row('Open Workbench', 'Dashboard, patterns, learning', 'layout', 'patternvault.openWorkbench');
    const items = [row('Backend', status + (this.backend.lastError && status === 'failed' ? ` — ${this.backend.lastError}` : ''), status === 'running' ? 'pass' : status === 'failed' ? 'error' : 'circle-slash', status === 'running' ? undefined : 'patternvault.startBackend')];
    const d = this.dashboard;
    if (status === 'running' && d) {
      items.push(
        row('Mode', modeLabel(d.mode), 'symbol-event', 'patternvault.switchMode'),
        row('Permissions', d.permission_level.replace(/_/g, ' '), 'shield', 'patternvault.setPermissionLevel'),
        row('Projects', String(d.counts.projects), 'folder', 'patternvault.registerWorkspace'),
        row('Captured sessions', String(d.counts.sessions), 'history'),
        row('Patterns', `${d.counts.patterns} (${d.counts.verified_patterns} verified)`, 'library'),
      );
    }
    items.unshift(open);
    return items;
  }
}
