/**
 * Spawns and supervises the local Python backend. No `vscode` import (unit-testable).
 */
import { ChildProcess, spawn as nodeSpawn, SpawnOptions } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { existsSync } from 'node:fs';
import { createServer } from 'node:net';
import * as path from 'node:path';

export type BackendStatus = 'stopped' | 'starting' | 'running' | 'failed';

export interface LaunchPlan {
  command: string;
  args: string[];
  env: NodeJS.ProcessEnv;
  cwd?: string;
}

export interface ResolveOptions {
  configured: string;
  managedPython?: string; // interpreter of the venv created by the Set Up Backend command
  searchRoots: string[]; // workspace folders and the repo root of a dev checkout
  exists?: (p: string) => boolean;
  platform?: NodeJS.Platform;
}

/** Pick the Python interpreter: explicit setting → managed venv → `<root>/.venv` of any search root → python3/python. */
export function resolvePython(opts: ResolveOptions): string {
  const exists = opts.exists ?? existsSync;
  const win = (opts.platform ?? process.platform) === 'win32';
  if (opts.configured.trim()) return opts.configured.trim();
  if (opts.managedPython && exists(opts.managedPython)) return opts.managedPython;
  for (const root of opts.searchRoots) {
    const candidate = win ? path.join(root, '.venv', 'Scripts', 'python.exe') : path.join(root, '.venv', 'bin', 'python');
    if (exists(candidate)) return candidate;
  }
  return win ? 'python' : 'python3';
}

export function buildLaunchPlan(python: string, port: number, token: string, home: string, baseEnv: NodeJS.ProcessEnv = process.env): LaunchPlan {
  const env: NodeJS.ProcessEnv = { ...baseEnv, PATTERNVAULT_TOKEN: token, PATTERNVAULT_PORT: String(port), PYTHONUNBUFFERED: '1' };
  if (home.trim()) env.PATTERNVAULT_HOME = home.trim();
  return { command: python, args: ['-m', 'app', '--port', String(port)], env };
}

export function generateToken(): string {
  return randomBytes(32).toString('base64url');
}

export function findFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = createServer();
    srv.unref();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const addr = srv.address();
      const port = typeof addr === 'object' && addr ? addr.port : 0;
      srv.close(() => resolve(port));
    });
  });
}

export interface ProcessDeps {
  spawnImpl?: (cmd: string, args: string[], opts: SpawnOptions) => ChildProcess;
  fetchImpl?: typeof fetch;
  log: (line: string) => void;
  sleep?: (ms: number) => Promise<void>;
}

export interface RunningBackend {
  baseUrl: string;
  token: string;
  pid: number | undefined;
}

export class BackendProcess {
  status: BackendStatus = 'stopped';
  lastError: string | undefined;
  private child: ChildProcess | undefined;
  private current: RunningBackend | undefined;
  private listeners = new Set<(s: BackendStatus) => void>();
  private stopping = false;

  constructor(private readonly deps: ProcessDeps) {}

  get running(): RunningBackend | undefined {
    return this.status === 'running' ? this.current : undefined;
  }

  onStatus(fn: (s: BackendStatus) => void): { dispose(): void } {
    this.listeners.add(fn);
    return { dispose: () => this.listeners.delete(fn) };
  }

  private setStatus(s: BackendStatus): void {
    if (s === this.status) return; // emit on change only
    this.status = s;
    for (const l of this.listeners) l(s);
  }

  async start(plan: LaunchPlan, port: number, timeoutMs: number): Promise<RunningBackend> {
    if (this.status === 'running' || this.status === 'starting') throw new Error('backend already running');
    this.stopping = false;
    this.lastError = undefined;
    this.setStatus('starting');
    const spawnFn = this.deps.spawnImpl ?? ((c, a, o) => nodeSpawn(c, a, o));
    const token = String(plan.env.PATTERNVAULT_TOKEN ?? '');
    const baseUrl = `http://127.0.0.1:${port}`;
    let child: ChildProcess;
    try {
      child = spawnFn(plan.command, plan.args, { env: plan.env, cwd: plan.cwd, stdio: ['ignore', 'pipe', 'pipe'] });
    } catch (err) {
      return this.fail(`failed to spawn ${plan.command}: ${(err as Error).message}`);
    }
    this.child = child;
    const exited = new Promise<{ code: number | null }>((resolve) => child.once('exit', (code) => resolve({ code })));
    child.on('error', (err) => {
      this.lastError = `process error: ${err.message}`;
      this.deps.log(this.lastError);
    });
    const pipe = (stream: NodeJS.ReadableStream | null | undefined, prefix: string) =>
      stream?.on('data', (b: Buffer) => {
        // Never log the token line from a manual launch; env-supplied tokens are not printed by the backend.
        for (const line of b.toString().split(/\r?\n/)) if (line.trim()) this.deps.log(`${prefix}${line}`);
      });
    pipe(child.stdout, '');
    pipe(child.stderr, '');

    void exited.then(({ code }) => {
      this.deps.log(`backend exited (code ${code})`);
      if (this.child === child) {
        this.child = undefined;
        this.current = undefined;
        if (!this.stopping && this.status !== 'failed') {
          this.lastError = `backend exited unexpectedly (code ${code})`;
          this.setStatus('failed');
        } else if (this.status !== 'failed') {
          this.setStatus('stopped');
        }
      }
    });

    const ok = await this.waitHealthy(baseUrl, timeoutMs, exited);
    if (!ok) {
      this.stopping = true;
      child.kill('SIGTERM');
      return this.fail(this.lastError ?? `backend did not become healthy within ${timeoutMs} ms`);
    }
    this.current = { baseUrl, token, pid: child.pid };
    this.setStatus('running');
    return this.current;
  }

  private fail(message: string): never {
    this.lastError = message;
    this.deps.log(message);
    this.setStatus('failed');
    throw new Error(message);
  }

  private async waitHealthy(baseUrl: string, timeoutMs: number, exited: Promise<{ code: number | null }>): Promise<boolean> {
    const fetchImpl = this.deps.fetchImpl ?? fetch;
    const sleep = this.deps.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
    const deadline = Date.now() + timeoutMs;
    let exitedEarly = false;
    void exited.then(() => (exitedEarly = true));
    while (Date.now() < deadline) {
      if (exitedEarly) {
        this.lastError = 'backend process exited during startup (see PatternVault log; is PatternVault installed in this Python environment?)';
        return false;
      }
      try {
        const res = await fetchImpl(`${baseUrl}/api/v1/health`, { signal: AbortSignal.timeout(1500) });
        if (res.ok) return true;
      } catch {
        /* not up yet */
      }
      await sleep(150);
    }
    return false;
  }

  async stop(graceMs = 4000): Promise<void> {
    const child = this.child;
    if (!child) {
      if (this.status !== 'failed') this.setStatus('stopped');
      return;
    }
    this.stopping = true;
    await new Promise<void>((resolve) => {
      const force = setTimeout(() => child.kill('SIGKILL'), graceMs);
      child.once('exit', () => {
        clearTimeout(force);
        resolve();
      });
      child.kill('SIGTERM');
    });
    this.current = undefined;
    this.child = undefined;
    this.setStatus('stopped');
  }
}
