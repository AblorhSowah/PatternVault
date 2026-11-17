/**
 * Clean-environment installation check against the REAL .vsix:
 *   1. fresh VS Code user-data + extensions directories (nothing of ours pre-installed)
 *   2. `code --install-extension <vsix>`
 *   3. launch VS Code and run test/packaged/suite inside the extension host: the *installed* extension must provision
 *      its own Python environment from the bundled wheel ("Set Up Backend") and then serve requests.
 * Needs network access to PyPI once (backend dependencies) and a Python 3.11+ interpreter (PV_SYSTEM_PYTHON or on PATH).
 */
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import * as path from 'node:path';
import { runTests } from '@vscode/test-electron';

async function main(): Promise<void> {
  delete process.env.ELECTRON_RUN_AS_NODE;
  const repo = path.resolve(__dirname, '..', '..', '..', '..');
  const vsix = readdirSync(path.join(repo, 'dist')).filter((f) => f.endsWith('.vsix')).map((f) => path.join(repo, 'dist', f))[0];
  if (!vsix) throw new Error('no .vsix in dist/: run scripts/package.sh first');
  const app = process.env.PV_VSCODE_APP ?? '/Applications/Visual Studio Code.app';
  const codeCli = path.join(app, 'Contents', 'Resources', 'app', 'bin', 'code');
  const exe = path.join(app, 'Contents', 'MacOS', 'Code');
  if (!existsSync(codeCli) || !existsSync(exe)) throw new Error(`VS Code not found at ${app} (set PV_VSCODE_APP)`);

  const tmp = mkdtempSync(path.join(tmpdir(), 'pv-clean-'));
  const userData = path.join(tmp, 'user-data');
  const extDir = path.join(tmp, 'extensions');
  const workspace = path.join(tmp, 'workspace');
  const dummy = path.join(tmp, 'dummy-dev-ext'); // the test host needs *a* development extension; PatternVault itself is loaded from extDir
  const home = path.join(tmp, 'pv-home');
  for (const d of [path.join(userData, 'User'), extDir, workspace, dummy]) mkdirSync(d, { recursive: true });
  writeFileSync(path.join(dummy, 'package.json'), JSON.stringify({ name: 'dummy', version: '0.0.0', publisher: 'x', engines: { vscode: '^1.100.0' } }));
  writeFileSync(path.join(workspace, 'calc.py'), 'def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n');

  console.log(`installing ${path.basename(vsix)} into a clean extensions dir…`);
  const out = execFileSync(codeCli, ['--install-extension', vsix, '--extensions-dir', extDir, '--user-data-dir', userData], { encoding: 'utf8', env: { ...process.env, ELECTRON_RUN_AS_NODE: undefined } });
  console.log(out.trim());

  const settings: Record<string, unknown> = {
    'patternvault.backend.home': home,
    'patternvault.backend.startupTimeoutMs': 90000,
    'security.workspace.trust.enabled': false,
    'update.mode': 'none',
    'telemetry.telemetryLevel': 'off',
  };
  if (process.env.PV_SYSTEM_PYTHON) settings['patternvault.backend.systemPython'] = process.env.PV_SYSTEM_PYTHON;
  writeFileSync(path.join(userData, 'User', 'settings.json'), JSON.stringify(settings));

  await runTests({
    vscodeExecutablePath: exe,
    extensionDevelopmentPath: dummy,
    extensionTestsPath: path.resolve(__dirname, 'suite', 'index'),
    extensionTestsEnv: { PV_EXT_DIR: extDir, PV_TEST_WORKSPACE: workspace, PV_TEST_HOME: home },
    launchArgs: [workspace, '--user-data-dir', userData, '--extensions-dir', extDir, '--disable-workspace-trust'],
  });
}

main().catch((err) => {
  console.error('Clean-install verification failed:', err);
  process.exit(1);
});
