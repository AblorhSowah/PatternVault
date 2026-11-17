import * as vscode from 'vscode';
import { registerCommands } from '../commands';
import { readSettings } from '../configuration/settings';
import { BackendManager } from '../services/backendManager';
import { ProviderKeyStore } from '../services/secrets';
import { CaptureUi } from '../views/captureUi';
import { StatusBar } from '../views/statusBar';
import { StatusTree } from '../views/statusTree';
import { WorkbenchPanel } from '../views/workbenchPanel';

export interface ExtensionApi {
  backend: BackendManager;
  keys: ProviderKeyStore;
  capture: CaptureUi;
  refresh(): Promise<void>;
}

export async function activate(context: vscode.ExtensionContext): Promise<ExtensionApi> {
  const output = vscode.window.createOutputChannel('PatternVault');
  const backend = new BackendManager(context, output);
  const statusBar = new StatusBar(backend);
  const tree = new StatusTree(backend);
  const workbench = new WorkbenchPanel(context, backend);
  const refreshRef: { fn: () => Promise<void> } = { fn: async () => undefined };
  const capture = new CaptureUi(backend, context, () => refreshRef.fn());

  const refresh = async (): Promise<void> => {
    if (backend.client && backend.status === 'running') {
      try {
        const d = await backend.client.dashboard();
        statusBar.dashboard = d;
        tree.dashboard = d;
      } catch (err) {
        output.appendLine(`dashboard refresh failed: ${(err as Error).message}`);
      }
    } else {
      statusBar.dashboard = undefined;
      tree.dashboard = undefined;
    }
    statusBar.render();
    tree.refresh();
    capture.render();
  };
  refreshRef.fn = refresh;

  context.subscriptions.push(
    output,
    backend,
    statusBar,
    capture,
    workbench,
    vscode.window.registerTreeDataProvider('patternvault.status', tree),
    backend.onDidChangeStatus(() => void refresh()),
    backend.onDidReceiveEvent((e) => {
      if (e.type === 'mode.changed' || e.type === 'config.changed' || e.type.startsWith('task.')) void refresh();
    }),
    ...registerCommands({ context, backend, output, refresh, capture, workbench }),
  );

  if (readSettings(vscode.workspace.getConfiguration('patternvault')).autoStart) {
    // Don't block activation on backend startup; surface failures in the status bar + log.
    void backend
      .start()
      .then(refresh)
      .catch((err: Error) => {
        output.appendLine(`auto-start failed: ${err.message}`);
        void vscode.window
          .showWarningMessage('PatternVault could not start its local backend (is it installed?).', 'Set Up Backend', 'Show Log')
          .then((choice) => {
            if (choice === 'Set Up Backend') void vscode.commands.executeCommand('patternvault.setupBackend');
            else if (choice === 'Show Log') output.show(true);
          });
      });
  }
  return { backend, keys: new ProviderKeyStore(context.secrets), capture, refresh };
}

export function deactivate(): void {
  /* disposables registered in activate() stop the backend */
}
