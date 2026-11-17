/**
 * Integration: the extension's BackendProcess + BackendClient + EventStream against the REAL Python backend
 * (`<repo>/.venv/bin/python -m app`). This verifies the extension↔backend contract end to end.
 * Skipped (visibly) when the repo virtualenv is not present.
 */
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import * as path from 'node:path';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { BackendClient, BackendEvent } from '../../src/services/backendClient';
import { CaptureController } from '../../src/services/captureController';
import { BackendProcess, buildLaunchPlan, findFreePort, generateToken, resolvePython } from '../../src/services/backendProcess';

const repoRoot = path.resolve(__dirname, '..', '..', '..');
const python = resolvePython({ configured: '', searchRoots: [repoRoot] });
const haveVenv = existsSync(path.join(repoRoot, '.venv'));

describe.skipIf(!haveVenv)('real backend', () => {
  let home: string;
  let proc: BackendProcess;
  let client: BackendClient;
  let token: string;
  let baseUrl: string;
  const logs: string[] = [];

  beforeAll(async () => {
    home = mkdtempSync(path.join(tmpdir(), 'pv-ext-'));
    const port = await findFreePort();
    token = generateToken();
    proc = new BackendProcess({ log: (l) => logs.push(l) });
    const running = await proc.start(buildLaunchPlan(python, port, token, home), port, 60000);
    baseUrl = running.baseUrl;
    client = new BackendClient({ baseUrl, token });
  }, 90000);

  afterAll(async () => {
    await proc?.stop();
    if (home) rmSync(home, { recursive: true, force: true });
  });

  it('health is public', async () => {
    const res = await fetch(`${baseUrl}/api/v1/health`);
    expect(((await res.json()) as { status: string }).status).toBe('ok');
  });

  it('rejects a client with the wrong token', async () => {
    const bad = new BackendClient({ baseUrl, token: 'wrong' });
    await expect(bad.dashboard()).rejects.toMatchObject({ status: 401 });
  });

  it('dashboard, mode switch and live event over WebSocket', async () => {
    const events: BackendEvent[] = [];
    const stream = client.connectEvents((e) => events.push(e));
    const d = await client.dashboard();
    expect(d.counts.patterns).toBe(0);
    await client.setMode('offline');
    await waitUntil(() => events.some((e) => e.type === 'mode.changed' && e.data.mode === 'offline'));
    expect((await client.dashboard()).mode).toBe('offline');
    stream.close();
  });

  it('rejects registering the data directory, accepts a normal folder', async () => {
    await expect(client.registerProject(home)).rejects.toMatchObject({ status: 422 });
    const other = mkdtempSync(path.join(tmpdir(), 'pv-proj-'));
    try {
      const p = await client.registerProject(other, 'proj');
      expect(p.id).toBeGreaterThan(0);
      expect((await client.listProjects()).map((x) => x.name)).toContain('proj');
    } finally {
      rmSync(other, { recursive: true, force: true });
    }
  });

  it('config validation errors surface with the server message', async () => {
    await expect(client.patchConfig({ privacy: { redact_secrets: false } })).rejects.toThrow(/redaction cannot be disabled/);
  });

  it('audit chain is intact after these operations', async () => {
    expect((await client.auditVerify()).ok).toBe(true);
  });

  it('capture window → learned pattern, end to end through the extension controller', async () => {
    const proj = mkdtempSync(path.join(tmpdir(), 'pv-cap-'));
    try {
      mkdirSync(path.join(proj, 'app'));
      writeFileSync(path.join(proj, 'requirements.txt'), 'fastapi==0.110.0\n');
      writeFileSync(path.join(proj, 'app', 'main.py'), 'from fastapi import FastAPI\napp = FastAPI()\n');
      execFileSync('git', ['init', '-q'], { cwd: proj });
      const p = await client.registerProject(proj, 'cap-proj');
      const kv = new Map<string, unknown>();
      const ctl = new CaptureController(() => client, { get: <T>(k: string) => kv.get(k) as T | undefined, update: async (k, v) => { kv.set(k, v); } });
      await ctl.start(p.id, 'add login');
      ctl.tracker.record('app/auth.py');
      writeFileSync(path.join(proj, 'app', 'auth.py'), 'import jwt\n\n\ndef create_access_token(uid):\n    return jwt.encode({"sub": uid}, "k", algorithm="HS256")\n\n\ndef login(user, password):\n    return create_access_token(user)\n');
      writeFileSync(path.join(proj, '.env'), 'SECRET=should-never-be-read-123456\n');
      const res = await ctl.end('passed');
      expect(res.changes).toBe(1); // auth.py only; .env is excluded
      expect(res.changedFilesSeen).toBe(1);
      expect(res.patterns[0]?.action).toBe('created');
      const found = await client.listPatterns({ q: 'create_access_token' });
      expect(found.total).toBe(1);
      expect(found.items[0]).toMatchObject({ status: 'observed', category: 'authentication' });
    } finally {
      rmSync(proj, { recursive: true, force: true });
    }
  });

  it('lists providers with honest capabilities', async () => {
    const provs = await client.providers();
    const byKey = Object.fromEntries(provs.map((x) => [x.key, x]));
    expect(byKey['claude-code']?.capabilities.captures_file_edits).toBe(true);
    expect(byKey['github-copilot']?.capabilities.captures_prompts).toBe(false);
  });

  it('backend binds to loopback only', async () => {
    expect(baseUrl.startsWith('http://127.0.0.1:')).toBe(true);
  });
});

async function waitUntil(cond: () => boolean, ms = 8000) {
  const end = Date.now() + ms;
  while (!cond()) {
    if (Date.now() > end) throw new Error('waitUntil timed out');
    await new Promise((r) => setTimeout(r, 50));
  }
}
