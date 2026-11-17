/**
 * Test plan: Workbench security boundary
 * - RPC allow-list: only whitelisted method+path pairs reach the backend; traversal/query smuggling rejected
 * - confirmations enforced HOST-side for deletes, high-risk permission changes, external transmission
 * - backend down / errors mapped; saveJson filename sanitised; command allow-list
 * - CSP is strict (nonce, no unsafe-inline/eval, default-src none)
 * - static scan of webview sources: no innerHTML-style sinks, eval, inline handlers
 * - contract: every API route literal used by the webview JS is permitted by the allow-list
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import * as path from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import type { BackendClient } from '../../src/services/backendClient';
import { BackendError } from '../../src/services/backendClient';
import { ALLOWED_COMMANDS, confirmationFor, handleRpc, isAllowed, RpcDeps, RpcMessage } from '../../src/services/webviewRpc';
import { makeNonce, renderWorkbenchHtml } from '../../src/views/webviewHtml';

function deps(over: Partial<RpcDeps> = {}, request = vi.fn(async () => ({ ok: 1 }))) {
  const client = { request } as unknown as BackendClient;
  const d: RpcDeps = {
    getClient: () => client,
    confirm: vi.fn(async () => true),
    saveJson: vi.fn(async () => true),
    runCommand: vi.fn(async () => undefined),
    ...over,
  };
  return { d, request };
}

describe('allow-list', () => {
  it.each([
    ['GET', '/dashboard'], ['GET', '/patterns'], ['GET', '/patterns/12'], ['POST', '/patterns/12/approve'], ['DELETE', '/patterns/3'],
    ['POST', '/providers/claude-code/import'], ['GET', '/providers/claude-code/sessions'], ['PUT', '/mode'], ['PATCH', '/config'], ['POST', '/search'],
    ['GET', '/audit'], ['GET', '/backups'], ['POST', '/backups/restore'], ['GET', '/storage'],
  ])('permits %s %s', (m, p) => expect(isAllowed(m, p)).toBe(true));

  it.each([
    ['POST', '/audit'], ['DELETE', '/audit'], ['POST', '/config/reset'], ['DELETE', '/projects/1'], ['POST', '/knowledge/import'], ['GET', '/patterns/../config'],
    ['GET', '/patterns/1?confirm=true'], ['GET', '/patterns//1'], ['GET', 'patterns'], ['DELETE', '/sessions/1'], ['POST', '/patterns/abc/approve'],
    ['GET', '/patterns/1/../../config'], ['PUT', '/config'], ['POST', '/patterns'], ['GET', '/openapi.json'], ['GET', '/patterns/1#x'],
  ])('rejects %s %s', (m, p) => expect(isAllowed(m, p)).toBe(false));

  it('forbidden calls never reach the backend', async () => {
    const { d, request } = deps();
    const res = await handleRpc({ type: 'api', id: 1, method: 'POST', path: '/config/reset' }, d);
    expect(res).toMatchObject({ ok: false, status: 403 });
    expect(request).not.toHaveBeenCalled();
  });
});

describe('confirmations', () => {
  it('delete needs confirmation; decline blocks the call', async () => {
    const confirm = vi.fn(async () => false);
    const { d, request } = deps({ confirm });
    const res = await handleRpc({ type: 'api', id: 2, method: 'DELETE', path: '/patterns/5', query: { confirm: true } }, d);
    expect(confirm).toHaveBeenCalledOnce();
    expect(res).toMatchObject({ ok: false, status: 499 });
    expect(request).not.toHaveBeenCalled();
  });
  it('accepted confirmation forwards with query', async () => {
    const { d, request } = deps();
    const res = await handleRpc({ type: 'api', id: 3, method: 'DELETE', path: '/patterns/5', query: { confirm: true } }, d);
    expect(res).toMatchObject({ ok: true });
    expect(request).toHaveBeenCalledWith('DELETE', '/patterns/5', undefined, { confirm: true });
  });
  it('privilege escalation and external transmission are confirmed host-side', () => {
    expect(confirmationFor('PATCH', '/config', { permission_level: 'restricted_autonomous' })).toMatch(/restricted autonomous/);
    expect(confirmationFor('PATCH', '/config', { permission_level: 'approved_execution' })).toBeTruthy();
    expect(confirmationFor('PATCH', '/config', { permission_level: 'read_only' })).toBeUndefined();
    expect(confirmationFor('PATCH', '/config', { cloud: { consent: { x: true } } })).toMatch(/external/);
    expect(confirmationFor('PATCH', '/config', { cloud: { consent: { x: false } } })).toBeUndefined();
    expect(confirmationFor('PATCH', '/config', { local_model: { allow_remote_host: true } })).toMatch(/non-local/);
    expect(confirmationFor('PATCH', '/config', { mode: 'offline' })).toBeUndefined();
  });
  it('starting tasks, running tests, approving actions and restoring files all need host-side confirmation', () => {
    expect(confirmationFor('POST', '/tasks', { kind: 'generate', request: 'Add login' })).toMatch(/Start a "generate" task.*Add login/);
    expect(confirmationFor('POST', '/testing/run', { project_id: 1 })).toMatch(/executes the project/);
    expect(confirmationFor('POST', '/approvals/5/decide', { approve: true })).toMatch(/Approve this agent action/);
    expect(confirmationFor('POST', '/approvals/5/decide', { approve: false })).toBeUndefined(); // denying is always safe
    expect(confirmationFor('POST', '/recovery-points/3/restore', undefined)).toMatch(/Restore files/);
    expect(confirmationFor('GET', '/tasks', undefined)).toBeUndefined();
    expect(confirmationFor('POST', '/backups/restore', { name: 'x' })).toMatch(/Restore this backup/);
    expect(confirmationFor('DELETE', '/backups/patternvault-1.pvbak', undefined)).toMatch(/delete this backup/);
    expect(isAllowed('DELETE', '/backups/patternvault-20260101-1.pvbak')).toBe(true);
    expect(isAllowed('DELETE', '/backups/../../etc/passwd')).toBe(false);
  });
  it('a compromised webview cannot start a task without the user seeing a prompt', async () => {
    const confirm = vi.fn(async () => false);
    const { d, request } = deps({ confirm });
    const res = await handleRpc({ type: 'api', id: 9, method: 'POST', path: '/tasks', body: { kind: 'generate', request: 'rm things' } }, d);
    expect(res).toMatchObject({ ok: false, status: 499 });
    expect(confirm).toHaveBeenCalledOnce();
    expect(request).not.toHaveBeenCalled();
  });
});

describe('error mapping and helpers', () => {
  it('maps backend errors and a missing backend', async () => {
    const failing = vi.fn(async () => { throw new BackendError('422: nope', 422); });
    expect(await handleRpc({ type: 'api', id: 1, method: 'GET', path: '/dashboard' }, deps({}, failing).d)).toMatchObject({ ok: false, status: 422, error: '422: nope' });
    expect(await handleRpc({ type: 'api', id: 1, method: 'GET', path: '/dashboard' }, deps({ getClient: () => undefined }).d)).toMatchObject({ ok: false, status: 503 });
  });
  it('sanitises save file names and enforces the command allow-list', async () => {
    const saveJson = vi.fn(async (_name: string, _data: unknown) => true);
    const { d } = deps({ saveJson });
    await handleRpc({ type: 'saveJson', id: 1, suggestedName: '../../etc/passwd.json', data: {} }, d);
    expect(saveJson.mock.calls[0]?.[0]).not.toContain('/');
    expect(await handleRpc({ type: 'command', id: 2, command: 'workbench.action.terminal.new' }, d)).toMatchObject({ ok: false });
    expect(await handleRpc({ type: 'command', id: 3, command: 'patternvault.startCapture' }, d)).toMatchObject({ ok: true });
    expect([...ALLOWED_COMMANDS].every((c) => c.startsWith('patternvault.'))).toBe(true);
  });
  it('ignores unknown message types', async () => {
    expect(await handleRpc({ type: 'weird', id: 1 } as unknown as RpcMessage, deps().d)).toBeUndefined();
  });
});

describe('CSP', () => {
  const html = renderWorkbenchHtml({ cspSource: 'vscode-webview://abc', nonce: 'N0nce', scriptUri: 'vscode-webview://abc/w.js', styleUri: 'vscode-webview://abc/w.css' });
  it('is strict', () => {
    const csp = /Content-Security-Policy" content="([^"]+)"/.exec(html)?.[1] ?? '';
    expect(csp).toContain("default-src 'none'");
    expect(csp).toContain("script-src 'nonce-N0nce'");
    expect(csp).not.toMatch(/unsafe-inline|unsafe-eval|\*/);
    expect(html).toContain('<script type="module" nonce="N0nce"');
    expect(html).not.toMatch(/\son[a-z]+=/i);
  });
  it('nonces are unique', () => {
    const rb = (n: number) => Uint8Array.from({ length: n }, () => Math.floor(Math.random() * 256));
    expect(makeNonce(rb)).not.toBe(makeNonce(rb));
    expect(makeNonce(rb).length).toBeGreaterThanOrEqual(20);
  });
});

function jsFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((f) => {
    const p = path.join(dir, f);
    return statSync(p).isDirectory() ? jsFiles(p) : p.endsWith('.js') ? [p] : [];
  });
}
const WEBVIEW_DIR = path.resolve(__dirname, '..', '..', 'media', 'webview');

describe('webview sources', () => {
  const files = jsFiles(WEBVIEW_DIR);
  it('exist', () => expect(files.length).toBeGreaterThanOrEqual(7));
  it.each(files.map((f) => [path.relative(WEBVIEW_DIR, f), f]))('%s has no HTML-injection or eval sinks', (_n, f) => {
    const src = readFileSync(f as string, 'utf8');
    for (const bad of [/\.innerHTML\b/, /\.outerHTML\b/, /insertAdjacentHTML/, /document\.write/, /\beval\s*\(/, /new\s+Function\b/, /javascript:/i, /setAttribute\(\s*['"]on/i, /srcdoc/]) {
      expect(bad.test(src), `${bad} in ${_n}`).toBe(false);
    }
  });
  it('every API route literal used by the UI is permitted by the host allow-list (UI/host contract)', () => {
    const used: [string, string][] = [];
    for (const f of files) {
      const src = readFileSync(f, 'utf8');
      for (const m of src.matchAll(/api\.(get|post|put|patch|del)\(\s*([`'"])([^`'"]+)\2/g)) {
        const method = m[1] === 'del' ? 'DELETE' : (m[1] as string).toUpperCase();
        const route = (m[3] as string).replace(/\$\{[^}]*(id|p\.key|key)[^}]*\}/gi, (x) => (/key/i.test(x) ? 'claude-code' : '12')).replace(/\$\{[^}]*\}/g, '12');
        used.push([method, route]);
      }
    }
    expect(used.length).toBeGreaterThan(15);
    const rejected = used.filter(([m, p]) => !isAllowed(m, p));
    expect(rejected).toEqual([]);
  });
});
