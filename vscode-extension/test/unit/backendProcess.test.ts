/**
 * Test plan: BackendProcess supervision using REAL child processes (node scripts standing in for the backend).
 * - resolvePython precedence; launch plan carries token/port/home via env, never argv
 * - healthy start → running; early exit → failed with actionable message; never-healthy → killed + failed
 * - stop() terminates the child; unexpected crash flips to failed
 * - findFreePort returns a bindable port; tokens are unique and long
 */
import { spawn } from 'node:child_process';
import { describe, expect, it } from 'vitest';
import { BackendProcess, buildLaunchPlan, findFreePort, generateToken, LaunchPlan, resolvePython } from '../../src/services/backendProcess';

describe('resolvePython', () => {
  it('prefers explicit setting, then .venv in roots, then python3', () => {
    expect(resolvePython({ configured: '/opt/py', searchRoots: ['/a'], exists: () => true, platform: 'linux' })).toBe('/opt/py');
    expect(resolvePython({ configured: '', searchRoots: ['/a', '/b'], exists: (p) => p === '/b/.venv/bin/python', platform: 'linux' })).toBe('/b/.venv/bin/python');
    expect(resolvePython({ configured: '', searchRoots: ['/a'], exists: () => false, platform: 'linux' })).toBe('python3');
    expect(resolvePython({ configured: '', searchRoots: ['C:\\p'], exists: () => false, platform: 'win32' })).toBe('python');
  });
});

describe('launch plan', () => {
  it('passes secrets through env only', () => {
    const t = generateToken();
    const plan = buildLaunchPlan('/py', 4321, t, '/data', { PATH: '/bin' });
    expect(plan.args).toEqual(['-m', 'app', '--port', '4321']);
    expect(plan.args.join(' ')).not.toContain(t);
    expect(plan.env.PATTERNVAULT_TOKEN).toBe(t);
    expect(plan.env.PATTERNVAULT_HOME).toBe('/data');
    expect(buildLaunchPlan('/py', 1, t, '  ', {}).env.PATTERNVAULT_HOME).toBeUndefined();
  });
  it('tokens are unique and long', () => {
    const a = generateToken(), b = generateToken();
    expect(a).not.toBe(b);
    expect(a.length).toBeGreaterThanOrEqual(40);
  });
  it('finds a free port', async () => {
    const p = await findFreePort();
    expect(p).toBeGreaterThan(1023);
  });
});

const nodeServer = (port: number, extra = ''): LaunchPlan => ({
  command: process.execPath,
  args: ['-e', `require('http').createServer((q,s)=>{s.statusCode=200;s.end('{"status":"ok"}')}).listen(${port},'127.0.0.1');${extra}`],
  env: { ...process.env, PATTERNVAULT_TOKEN: 'tok' },
});

function proc(logs: string[] = []) {
  return new BackendProcess({ log: (l) => logs.push(l) });
}

describe('BackendProcess', () => {
  it('starts, reports running, stops', async () => {
    const port = await findFreePort();
    const p = proc();
    const statuses: string[] = [];
    p.onStatus((s) => statuses.push(s));
    const running = await p.start(nodeServer(port), port, 10000);
    expect(p.status).toBe('running');
    expect(running.baseUrl).toBe(`http://127.0.0.1:${port}`);
    expect(running.token).toBe('tok');
    const pid = running.pid!;
    await p.stop();
    expect(p.status).toBe('stopped');
    expect(statuses).toEqual(['starting', 'running', 'stopped']);
    expect(() => process.kill(pid, 0)).toThrow(); // really gone
  });

  it('fails with an actionable message when the process exits during startup', async () => {
    const port = await findFreePort();
    const p = proc();
    await expect(p.start({ command: process.execPath, args: ['-e', 'process.exit(3)'], env: {} }, port, 5000)).rejects.toThrow(/exited during startup/);
    expect(p.status).toBe('failed');
    expect(p.lastError).toMatch(/PatternVault installed/);
  });

  it('fails when the command cannot be spawned', async () => {
    const port = await findFreePort();
    const p = proc();
    await expect(p.start({ command: '/definitely/not/here', args: [], env: {} }, port, 3000)).rejects.toThrow();
    expect(p.status).toBe('failed');
  });

  it('times out, kills the child and reports failure', async () => {
    const port = await findFreePort();
    const p = proc();
    const t0 = Date.now();
    await expect(p.start({ command: process.execPath, args: ['-e', 'setInterval(()=>{},1000)'], env: {} }, port, 1200)).rejects.toThrow(/did not become healthy/);
    expect(Date.now() - t0).toBeLessThan(6000);
    expect(p.status).toBe('failed');
  });

  it('detects an unexpected crash after startup', async () => {
    const port = await findFreePort();
    const p = proc();
    const running = await p.start(nodeServer(port), port, 10000);
    process.kill(running.pid!, 'SIGKILL');
    await waitUntil(() => p.status === 'failed');
    expect(p.lastError).toMatch(/exited unexpectedly/);
  });

  it('refuses to start twice', async () => {
    const port = await findFreePort();
    const p = proc();
    await p.start(nodeServer(port), port, 10000);
    await expect(p.start(nodeServer(port), port, 1000)).rejects.toThrow(/already running/);
    await p.stop();
  });

  it('forwards child output to the log', async () => {
    const port = await findFreePort();
    const logs: string[] = [];
    const p = proc(logs);
    await p.start(nodeServer(port, "console.log('hello-from-backend');"), port, 10000);
    await waitUntil(() => logs.some((l) => l.includes('hello-from-backend')));
    await p.stop();
  });
});

async function waitUntil(cond: () => boolean, ms = 5000) {
  const end = Date.now() + ms;
  while (!cond()) {
    if (Date.now() > end) throw new Error('waitUntil timed out');
    await new Promise((r) => setTimeout(r, 25));
  }
}
void spawn;
