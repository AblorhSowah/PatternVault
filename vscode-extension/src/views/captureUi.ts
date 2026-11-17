import * as vscode from 'vscode';
import type { BackendManager } from '../services/backendManager';
import { CaptureController } from '../services/captureController';
import { detectAssistants } from '../providers/assistantDetector';

/** Wires file watching, the status-bar capture indicator and capture/import commands to the controller. */
export class CaptureUi implements vscode.Disposable {
  readonly controller: CaptureController;
  private readonly item: vscode.StatusBarItem;
  private readonly disposables: vscode.Disposable[] = [];

  constructor(
    private readonly backend: BackendManager,
    context: vscode.ExtensionContext,
    private readonly refresh: () => Promise<void>,
  ) {
    this.controller = new CaptureController(() => backend.client, {
      get: (k) => context.workspaceState.get(k),
      update: (k, v) => context.workspaceState.update(k, v),
    });
    this.item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 49);
    this.item.command = 'patternvault.endCapture';
    const watcher = vscode.workspace.createFileSystemWatcher('**/*');
    const onChange = (uri: vscode.Uri) => {
      const rel = vscode.workspace.asRelativePath(uri, false);
      if (this.controller.tracker.record(rel)) this.render();
    };
    this.disposables.push(watcher, watcher.onDidChange(onChange), watcher.onDidCreate(onChange), watcher.onDidDelete(onChange), this.item);
    this.render();
  }

  render(): void {
    const cur = this.controller.current;
    if (!cur) {
      this.item.hide();
      return;
    }
    this.item.text = `$(record) Capturing · ${this.controller.tracker.count} file(s)`;
    this.item.tooltip = `PatternVault capture "${cur.title}" — click to end and extract patterns`;
    this.item.show();
  }

  private async pickProject(): Promise<{ id: number; name: string } | undefined> {
    const client = this.backend.client;
    if (!client) throw new Error('backend is not running');
    const folders = vscode.workspace.workspaceFolders ?? [];
    if (!folders.length) throw new Error('open a folder first');
    const projects = await client.listProjects();
    const fs = (p: string) => p.replace(/\/+$/, '');
    const matching = projects.filter((p) => folders.some((f) => fs(f.uri.fsPath) === fs(p.root_path)));
    if (matching.length === 0) {
      for (const f of folders) await client.registerProject(f.uri.fsPath, f.name);
      return this.pickProject();
    }
    if (matching.length === 1) return matching[0];
    const chosen = await vscode.window.showQuickPick(matching.map((p) => ({ label: p.name, description: p.root_path, id: p.id })), { title: 'Project' });
    return chosen ? { id: chosen.id, name: chosen.label } : undefined;
  }

  async start(): Promise<void> {
    const project = await this.pickProject();
    if (!project) return;
    const title = await vscode.window.showInputBox({ title: 'What are you about to build?', prompt: 'Short description (becomes the capture title)', value: '' });
    if (title === undefined) return;
    await this.controller.start(project.id, title || 'Capture window');
    this.render();
    void vscode.window.showInformationMessage('PatternVault is capturing file changes. Work with your AI assistant, then run "PatternVault: End Capture".');
    await this.refresh();
  }

  async end(): Promise<void> {
    if (!this.controller.current) throw new Error('no active capture session');
    const outcome = await vscode.window.showQuickPick(
      [
        { label: 'Tests passed / it works', value: 'passed' as const },
        { label: 'It failed / was abandoned', value: 'failed' as const },
        { label: 'Not sure', value: 'unknown' as const },
      ],
      { title: 'How did it go? (recorded as an observation, not verification)' },
    );
    if (!outcome) return;
    const res = await this.controller.end(outcome.value);
    this.render();
    const created = res.patterns.filter((p) => p.action !== 'duplicate').length;
    void vscode.window.showInformationMessage(`PatternVault: ${res.changes} file change(s) captured, ${created} pattern(s) learned (review them before trusting).`);
    await this.refresh();
  }

  async importSessions(): Promise<void> {
    const client = this.backend.client;
    if (!client) throw new Error('backend is not running');
    const project = await this.pickProject();
    if (!project) return;
    const providers = (await client.providers()).filter((p) => p.available && p.capabilities.captures_prompts && p.key !== 'manual');
    if (!providers.length) {
      void vscode.window.showInformationMessage('No supported assistant logs found on this machine. Use "Start Capture" for workspace capture.');
      return;
    }
    const items: (vscode.QuickPickItem & { provider: string; external: string })[] = [];
    for (const p of providers) {
      for (const s of await client.providerSessions(p.key, project.id)) {
        items.push({ label: s.title || s.external_id, description: `${p.display_name} · ${s.message_count} messages`, detail: s.started_at ?? '', provider: p.key, external: s.external_id });
      }
    }
    if (!items.length) {
      void vscode.window.showInformationMessage('No assistant sessions found for this project.');
      return;
    }
    const picked = await vscode.window.showQuickPick(items, { canPickMany: true, title: 'Import AI assistant sessions (read from your local logs)' });
    if (!picked?.length) return;
    let patterns = 0;
    for (const p of picked) {
      const r = await client.importSession(p.provider, project.id, p.external);
      patterns += (r.patterns ?? []).filter((x) => x.action !== 'duplicate').length;
    }
    void vscode.window.showInformationMessage(`PatternVault: imported ${picked.length} session(s), learned ${patterns} pattern(s).`);
    await this.refresh();
  }

  async showProviders(): Promise<void> {
    const client = this.backend.client;
    if (!client) throw new Error('backend is not running');
    const infos = detectAssistants(vscode.extensions.all.map((e) => e.id), await client.providers());
    const pick = infos.map((a) => ({ label: `${a.installed ? '$(check)' : '$(circle-slash)'} ${a.name}`, description: a.capture, detail: a.summary }));
    await vscode.window.showQuickPick(pick, { title: 'Supported AI assistants and what PatternVault can capture' });
  }

  async analyze(): Promise<void> {
    const client = this.backend.client;
    if (!client) throw new Error('backend is not running');
    const project = await this.pickProject();
    if (!project) return;
    const a = await client.analyzeProject(project.id);
    void vscode.window.showInformationMessage(`PatternVault: ${project.name} — ${a.primary_language ?? 'unknown language'}, ${a.frameworks.map((f) => `${f.name} ${f.version}`.trim()).join(', ') || 'no frameworks detected'} (${a.file_count} files)`);
  }

  dispose(): void {
    for (const d of this.disposables) d.dispose();
  }
}
