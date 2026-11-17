/**
 * Extension-host test plan (runs inside real VS Code):
 * - activates; every command in package.json is registered and equals COMMAND_IDS
 * - auto-starts the real backend; status becomes running; dashboard reachable
 * - registerWorkspace registers the open folder with the backend
 * - the chat view opens, and the chat answers through the extension's own authenticated client (no model → honest stored-pattern answer)
 * - SecretStorage round-trip works and the key never reaches the backend config
 * - stopBackend stops it; restart brings it back
 */
import * as assert from 'node:assert';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as vscode from 'vscode';
import { COMMAND_IDS } from '../../src/commands';
import type { ExtensionApi } from '../../src/extension/extension';

const EXT_ID = 'patternvault.patternvault';

async function waitFor(cond: () => boolean | Promise<boolean>, ms = 60000, what = 'condition'): Promise<void> {
  const end = Date.now() + ms;
  while (!(await cond())) {
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 100));
  }
}

describe('PatternVault extension host', () => {
  let api: ExtensionApi;

  before(async () => {
    const ext = vscode.extensions.getExtension<ExtensionApi>(EXT_ID);
    assert.ok(ext, 'extension not found');
    api = await ext.activate();
  });

  it('registers every manifest command', async () => {
    const manifest = JSON.parse(fs.readFileSync(path.resolve(__dirname, '..', '..', '..', 'package.json'), 'utf8'));
    const declared: string[] = manifest.contributes.commands.map((c: { command: string }) => c.command).sort();
    assert.deepStrictEqual(declared, [...COMMAND_IDS].sort());
    const registered = new Set(await vscode.commands.getCommands(true));
    for (const id of declared) assert.ok(registered.has(id), `command not registered: ${id}`);
  });

  it('auto-starts the real backend', async () => {
    await waitFor(() => api.backend.status === 'running', 90000, 'backend running');
    const d = await api.backend.client!.dashboard();
    assert.strictEqual(d.counts.patterns, 0);
    assert.strictEqual(d.mode, 'learning');
  });

  it('registers the open workspace folder', async () => {
    await vscode.commands.executeCommand('patternvault.registerWorkspace');
    const projects = await api.backend.client!.listProjects();
    const ws = process.env.PV_TEST_WORKSPACE!;
    assert.ok(projects.some((p) => fs.realpathSync(p.root_path) === fs.realpathSync(ws)), 'workspace not registered');
  });

  it('opens the chat view and the chat works through the extension client', async () => {
    await vscode.commands.executeCommand('patternvault.openChat'); // resolves the side-bar webview view
    const client = api.backend.client!;
    const conv = (await client.request('POST', '/chat/conversations', {})) as { id: number };
    const sent = (await client.request('POST', `/chat/conversations/${conv.id}/messages`, { message: 'How do we do jwt login?' })) as { turn_id: number };
    let turn: { status: string; text: string; meta: { model_used?: boolean } } = { status: 'running', text: '', meta: {} };
    await waitFor(async () => {
      turn = (await client.request('GET', `/chat/turns/${sent.turn_id}`)) as typeof turn;
      return turn.status !== 'running';
    }, 30000, 'chat turn to finish');
    assert.strictEqual(turn.status, 'done');
    assert.strictEqual(turn.meta.model_used, false, 'no local model is configured in this test, so it must not pretend one replied');
    assert.match(turn.text, /can't reach a language model/);
    const stored = (await client.request('GET', `/chat/conversations/${conv.id}`)) as { messages: { role: string }[] };
    assert.deepStrictEqual(stored.messages.map((m) => m.role), ['user', 'assistant']);
    await client.request('DELETE', `/chat/conversations/${conv.id}`);
  });

  it('stores keys in SecretStorage only', async () => {
    const secret = 'sk-test-' + 'z'.repeat(30);
    await api.keys.set('unit-provider', secret);
    assert.strictEqual(await api.keys.get('unit-provider'), secret);
    const cfg = JSON.stringify(await api.backend.client!.getConfig());
    assert.ok(!cfg.includes(secret), 'secret leaked into backend config');
    const home = process.env.PV_TEST_HOME!;
    const cfgPath = path.join(home, 'config.json');
    if (fs.existsSync(cfgPath)) assert.ok(!fs.readFileSync(cfgPath, 'utf8').includes(secret));
    await api.keys.clear('unit-provider');
    assert.strictEqual(await api.keys.get('unit-provider'), undefined);
  });

  it('stop and restart work', async () => {
    await vscode.commands.executeCommand('patternvault.stopBackend');
    assert.strictEqual(api.backend.status, 'stopped');
    await vscode.commands.executeCommand('patternvault.startBackend');
    await waitFor(() => api.backend.status === 'running', 90000, 'backend restart');
    assert.ok((await api.backend.client!.health()).status === 'ok');
  });

  after(async () => {
    await api.backend.stop();
  });
});
