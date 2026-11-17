/**
 * Launches the REAL VS Code (the locally installed one, or a downloaded stable build) with the extension
 * loaded in development mode and runs the Mocha suite in test/suite inside the extension host.
 */
import { runTests, downloadAndUnzipVSCode } from '@vscode/test-electron';
import { existsSync, mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import * as path from 'node:path';

async function main(): Promise<void> {
  // When launched from a terminal inside VS Code this is inherited and turns Electron into plain Node.
  delete process.env.ELECTRON_RUN_AS_NODE;
  const extensionDevelopmentPath = path.resolve(__dirname, '..', '..');
  const extensionTestsPath = path.resolve(__dirname, 'suite', 'index');
  const repoRoot = path.resolve(extensionDevelopmentPath, '..');

  const tmp = mkdtempSync(path.join(tmpdir(), 'pv-host-'));
  const userData = path.join(tmp, 'user-data');
  const extDir = path.join(tmp, 'extensions');
  const workspace = path.join(tmp, 'workspace');
  const pvHome = path.join(tmp, 'pv-home');
  mkdirSync(path.join(userData, 'User'), { recursive: true });
  mkdirSync(workspace, { recursive: true });
  writeFileSync(path.join(workspace, 'hello.py'), 'print("hi")\n');
  // "machine"-scoped settings must come from user settings, not the workspace.
  const python = path.join(repoRoot, '.venv', 'bin', 'python');
  writeFileSync(
    path.join(userData, 'User', 'settings.json'),
    JSON.stringify({
      'patternvault.backend.pythonPath': python,
      'patternvault.backend.home': pvHome,
      'patternvault.backend.startupTimeoutMs': 60000,
      'security.workspace.trust.enabled': false,
      'update.mode': 'none',
      'telemetry.telemetryLevel': 'off',
    }),
  );

  const localApp = '/Applications/Visual Studio Code.app/Contents/MacOS/Code';
  const vscodeExecutablePath = process.env.PV_VSCODE_PATH
    ? process.env.PV_VSCODE_PATH
    : existsSync(localApp)
      ? localApp
      : await downloadAndUnzipVSCode('stable');

  await runTests({
    vscodeExecutablePath,
    extensionDevelopmentPath,
    extensionTestsPath,
    extensionTestsEnv: { PV_TEST_WORKSPACE: workspace, PV_TEST_HOME: pvHome },
    launchArgs: [workspace, '--disable-extensions', '--user-data-dir', userData, '--extensions-dir', extDir, '--disable-workspace-trust'],
  });
}

main().catch((err) => {
  console.error('Extension host tests failed:', err);
  process.exit(1);
});
