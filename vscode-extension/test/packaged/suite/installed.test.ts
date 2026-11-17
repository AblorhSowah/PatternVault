/** Runs inside VS Code against the extension INSTALLED from the .vsix into a clean profile. */
import * as assert from 'node:assert';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as vscode from 'vscode';

const EXT_ID = 'patternvault.patternvault';
type Api = { backend: { status: string; client?: { health(): Promise<{ version: string; status: string }>; dashboard(): Promise<{ counts: { patterns: number } }>; registerProject(p: string, n?: string): Promise<{ id: number }>; startSession(id: number, t: string): Promise<{ id: number }>; endSession(id: number, o: 'passed'): Promise<{ changes: number; patterns?: unknown[] }>; listPatterns(q?: Record<string, string>): Promise<{ total: number }> }; lastPython?: string; venvDir: string } };

async function waitFor(cond: () => boolean, ms: number, what: string): Promise<void> {
  const end = Date.now() + ms;
  while (!cond()) {
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 500));
  }
}

describe('PatternVault installed from the VSIX into a clean VS Code profile', function () {
  let api: Api;

  it('is loaded from the installed extensions directory (not from a dev checkout)', async () => {
    const ext = vscode.extensions.getExtension<Api>(EXT_ID);
    assert.ok(ext, 'extension is not installed');
    const extDir = process.env.PV_EXT_DIR as string;
    assert.ok(path.resolve(ext.extensionPath).startsWith(path.resolve(extDir)), `loaded from ${ext.extensionPath}, expected under ${extDir}`);
    assert.ok(fs.readdirSync(path.join(ext.extensionPath, 'backend')).some((f) => f.endsWith('.whl')), 'bundled wheel missing from the VSIX');
    api = await ext.activate();
  });

  it('has no backend before setup (nothing installed on this machine)', async () => {
    await new Promise((r) => setTimeout(r, 4000)); // let the auto-start attempt fail
    assert.notStrictEqual(api.backend.status, 'running', 'a backend was running before Set Up Backend');
    assert.ok(!fs.existsSync(api.backend.venvDir), 'managed venv should not exist yet');
  });

  it('Set Up Backend provisions a private environment from the bundled wheel and starts the backend', async () => {
    await vscode.commands.executeCommand('patternvault.setupBackend');
    await waitFor(() => api.backend.status === 'running', 120000, 'backend running after setup');
    assert.ok(fs.existsSync(path.join(api.backend.venvDir, 'pyvenv.cfg')), 'venv was not created');
    assert.ok(api.backend.lastPython && api.backend.lastPython.startsWith(api.backend.venvDir), `backend used ${api.backend.lastPython}, not the managed venv`);
  });

  it('the installed backend serves requests and reports the packaged version', async () => {
    const h = await api.backend.client!.health();
    assert.strictEqual(h.status, 'ok');
    assert.strictEqual(h.version, '0.1.0');
    assert.strictEqual((await api.backend.client!.dashboard()).counts.patterns, 0);
  });

  it('learns from a capture window end to end (migrations, tree-sitter, FTS all work from the wheel)', async () => {
    const ws = process.env.PV_TEST_WORKSPACE as string;
    const c = api.backend.client!;
    const p = await c.registerProject(ws, 'clean-ws');
    const s = await c.startSession(p.id, 'add multiply');
    fs.writeFileSync(path.join(ws, 'calc.py'), 'def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n\n\ndef multiply(a, b):\n    return a * b\n\n\ndef divide(a, b):\n    if b == 0:\n        raise ValueError("division by zero")\n    return a / b\n');
    const end = await c.endSession(s.id, 'passed');
    assert.strictEqual(end.changes, 1);
    assert.ok((end.patterns ?? []).length >= 1);
    assert.strictEqual((await c.listPatterns({ q: 'divide multiply' })).total, 1);
  });
});
