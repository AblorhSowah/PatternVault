import * as vscode from 'vscode';
import type { BackendManager } from '../services/backendManager';
import { ProviderKeyStore } from '../services/secrets';
import type { Mode, PermissionLevel } from '../services/backendClient';
import { modeLabel } from '../services/statusModel';
import type { CaptureUi } from '../views/captureUi';
import type { WorkbenchPanel } from '../views/workbenchPanel';

export interface CommandDeps {
  context: vscode.ExtensionContext;
  backend: BackendManager;
  output: vscode.OutputChannel;
  refresh: () => Promise<void>;
  capture: CaptureUi;
  workbench: WorkbenchPanel;
}

function fail(output: vscode.OutputChannel, what: string, err: unknown): void {
  const msg = err instanceof Error ? err.message : String(err);
  output.appendLine(`${what}: ${msg}`);
  void vscode.window.showErrorMessage(`PatternVault: ${what}: ${msg}`);
}

export const COMMAND_IDS = [
  'patternvault.startBackend',
  'patternvault.stopBackend',
  'patternvault.restartBackend',
  'patternvault.showLog',
  'patternvault.switchMode',
  'patternvault.setPermissionLevel',
  'patternvault.registerWorkspace',
  'patternvault.setProviderKey',
  'patternvault.clearProviderKey',
  'patternvault.refreshStatus',
  'patternvault.startCapture',
  'patternvault.endCapture',
  'patternvault.importSessions',
  'patternvault.showProviders',
  'patternvault.analyzeProject',
  'patternvault.openWorkbench',
  'patternvault.setupBackend',
] as const;

export function registerCommands(deps: CommandDeps): vscode.Disposable[] {
  const { context, backend, output, refresh, capture, workbench } = deps;
  const keys = new ProviderKeyStore(context.secrets);
  const reg = (id: (typeof COMMAND_IDS)[number], fn: () => Promise<void> | void) =>
    vscode.commands.registerCommand(id, async () => {
      try {
        await fn();
      } catch (err) {
        fail(output, id.replace('patternvault.', ''), err);
      }
    });

  const needClient = () => {
    if (!backend.client) throw new Error('backend is not running (run "PatternVault: Start Backend")');
    return backend.client;
  };

  return [
    reg('patternvault.startBackend', async () => {
      await backend.start();
      await refresh();
    }),
    reg('patternvault.stopBackend', async () => {
      await backend.stop();
      await refresh();
    }),
    reg('patternvault.restartBackend', async () => {
      await backend.restart();
      await refresh();
    }),
    reg('patternvault.showLog', () => output.show(true)),
    reg('patternvault.refreshStatus', refresh),
    reg('patternvault.openWorkbench', () => workbench.show()),
    reg('patternvault.setupBackend', async () => {
      await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: 'PatternVault: setting up the backend', cancellable: false }, async (progress) => {
        await backend.setup((m) => progress.report({ message: m }));
        progress.report({ message: 'Starting the backend…' });
        await backend.start();
      });
      await refresh();
      void vscode.window.showInformationMessage('PatternVault backend is ready.');
    }),
    reg('patternvault.startCapture', () => capture.start()),
    reg('patternvault.endCapture', () => capture.end()),
    reg('patternvault.importSessions', () => capture.importSessions()),
    reg('patternvault.showProviders', () => capture.showProviders()),
    reg('patternvault.analyzeProject', () => capture.analyze()),
    reg('patternvault.switchMode', async () => {
      const client = needClient();
      const picks: { label: string; description: string; mode: Mode }[] = [
        { label: 'Learning', description: 'Observe cloud AI assistants and capture knowledge', mode: 'learning' },
        { label: 'Offline', description: 'Local knowledge + local model only, no network use', mode: 'offline' },
        { label: 'Hybrid', description: 'Use cloud and local AI per your policy (cloud needs consent)', mode: 'hybrid' },
      ];
      const chosen = await vscode.window.showQuickPick(picks, { title: 'PatternVault: operating mode' });
      if (!chosen) return;
      await client.setMode(chosen.mode);
      void vscode.window.setStatusBarMessage(`PatternVault mode: ${modeLabel(chosen.mode)}`, 3000);
      await refresh();
    }),
    reg('patternvault.setPermissionLevel', async () => {
      const client = needClient();
      const picks: { label: string; description: string; level: PermissionLevel }[] = [
        { label: 'Read-only', description: 'Agents may only read', level: 'read_only' },
        { label: 'Assisted', description: 'Approve every file change and command', level: 'assisted' },
        { label: 'Approved execution', description: 'Approve a plan once; safe steps then run', level: 'approved_execution' },
        { label: 'Restricted autonomous', description: 'Safe in-project edits and tests run on their own', level: 'restricted_autonomous' },
      ];
      const chosen = await vscode.window.showQuickPick(picks, { title: 'PatternVault: agent permission level' });
      if (!chosen) return;
      if (chosen.level === 'restricted_autonomous') {
        const ok = await vscode.window.showWarningMessage(
          'Restricted autonomous mode lets agents edit project files and run allow-listed test commands without asking each time. Destructive commands stay blocked and recovery points are always taken.',
          { modal: true },
          'Enable',
        );
        if (ok !== 'Enable') return;
      }
      await client.patchConfig({ permission_level: chosen.level });
      await refresh();
    }),
    reg('patternvault.registerWorkspace', async () => {
      const client = needClient();
      const folders = vscode.workspace.workspaceFolders ?? [];
      if (!folders.length) throw new Error('open a folder first');
      for (const f of folders) await client.registerProject(f.uri.fsPath, f.name);
      void vscode.window.showInformationMessage(`PatternVault: registered ${folders.length} project(s)`);
      await refresh();
    }),
    reg('patternvault.setProviderKey', async () => {
      const provider = await vscode.window.showInputBox({ title: 'Provider id', prompt: 'e.g. openai-compatible', ignoreFocusOut: true });
      if (!provider) return;
      const key = await vscode.window.showInputBox({ title: `API key for ${provider}`, password: true, ignoreFocusOut: true });
      if (!key) return;
      await keys.set(provider, key);
      void vscode.window.showInformationMessage(`PatternVault: key for "${provider}" stored in SecretStorage`);
    }),
    reg('patternvault.clearProviderKey', async () => {
      const provider = await vscode.window.showInputBox({ title: 'Provider id to remove' });
      if (!provider) return;
      await keys.clear(provider);
      void vscode.window.showInformationMessage(`PatternVault: key for "${provider}" removed`);
    }),
  ];
}
