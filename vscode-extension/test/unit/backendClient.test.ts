/**
 * Test plan: BackendClient against a real local HTTP server (not a fetch mock) + EventStream against a real `ws` server.
 * - sends bearer token + JSON; maps FastAPI `detail` errors (string and validation-array) to BackendError
 * - timeout → BackendError(status 0); connection refused → BackendError(status 0)
 * - EventStream authenticates first, delivers events, reconnects after server drop, stops on close()
 */
import { createServer, IncomingMessage, Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { WebSocketServer } from 'ws';
import { BackendClient, BackendError, BackendEvent } from '../../src/services/backendClient';

let server: Server;
let base: string;
let lastReq: { method?: string; url?: string; auth?: string; body?: string } = {};
let handler: (req: IncomingMessage, res: import('node:http').ServerResponse) => void;

beforeEach(async () => {
  lastReq = {};
  handler = (_req, res) => { res.setHeader('content-type', 'application/json'); res.end('{"ok":true}'); };
  server = createServer((req, res) => {
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', () => {
      lastReq = { method: req.method, url: req.url, auth: req.headers.authorization, body };
      handler(req, res);
    });
  });
  await new Promise<void>((r) => server.listen(0, '127.0.0.1', r));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
afterEach(() => new Promise<void>((r) => server.close(() => r())));

describe('BackendClient', () => {
  it('sends token, JSON body and query params', async () => {
    const c = new BackendClient({ baseUrl: base, token: 'tok123' });
    await c.request('POST', '/projects', { root_path: '/x' }, { confirm: true, skip: undefined });
    expect(lastReq.method).toBe('POST');
    expect(lastReq.url).toBe('/api/v1/projects?confirm=true');
    expect(lastReq.auth).toBe('Bearer tok123');
    expect(JSON.parse(lastReq.body!)).toEqual({ root_path: '/x' });
  });

  it('maps string detail errors', async () => {
    handler = (_q, res) => { res.statusCode = 422; res.end(JSON.stringify({ detail: 'bad path' })); };
    const err = await new BackendClient({ baseUrl: base, token: 't' }).setMode('offline').catch((e) => e);
    expect(err).toBeInstanceOf(BackendError);
    expect(err.status).toBe(422);
    expect(err.message).toBe('422: bad path');
  });

  it('maps validation-array errors', async () => {
    handler = (_q, res) => { res.statusCode = 422; res.end(JSON.stringify({ detail: [{ msg: 'a' }, { msg: 'b' }] })); };
    const err = await new BackendClient({ baseUrl: base, token: 't' }).getConfig().catch((e) => e);
    expect(err.message).toBe('422: a; b');
  });

  it('401 surfaces as BackendError', async () => {
    handler = (_q, res) => { res.statusCode = 401; res.end('{"detail":"missing or invalid bearer token"}'); };
    const err = await new BackendClient({ baseUrl: base, token: 'x' }).dashboard().catch((e) => e);
    expect(err.status).toBe(401);
  });

  it('times out', async () => {
    handler = () => { /* never respond */ };
    const err = await new BackendClient({ baseUrl: base, token: 't', timeoutMs: 100 }).health().catch((e) => e);
    expect(err).toBeInstanceOf(BackendError);
    expect(err.status).toBe(0);
    expect(err.message).toMatch(/timed out/);
  });

  it('reports unreachable backend', async () => {
    const err = await new BackendClient({ baseUrl: 'http://127.0.0.1:1', token: 't', timeoutMs: 2000 }).health().catch((e) => e);
    expect(err.status).toBe(0);
    expect(err.message).toMatch(/Cannot reach backend/);
  });
});

describe('EventStream', () => {
  it('authenticates, receives events and reconnects after a drop', async () => {
    const wss = new WebSocketServer({ host: '127.0.0.1', port: 0, path: '/api/v1/ws' });
    await new Promise<void>((r) => wss.on('listening', () => r()));
    const port = (wss.address() as AddressInfo).port;
    const authTokens: string[] = [];
    wss.on('connection', (ws) => {
      ws.once('message', (m) => {
        const msg = JSON.parse(m.toString());
        authTokens.push(msg.token);
        ws.send(JSON.stringify({ type: 'ready', seq: 0 }));
        ws.send(JSON.stringify({ seq: 1, type: 'task.started', ts: 'now', data: { id: 7 } }));
      });
    });
    const events: BackendEvent[] = [];
    const states: boolean[] = [];
    const client = new BackendClient({ baseUrl: `http://127.0.0.1:${port}`, token: 'secret-t' });
    const stream = client.connectEvents((e) => events.push(e), (s) => states.push(s));
    await waitFor(() => events.length === 1);
    expect(events[0]?.data).toEqual({ id: 7 });
    expect(authTokens).toEqual(['secret-t']);
    expect(states).toContain(true);
    // drop all connections → expect a reconnect (second auth)
    for (const c of wss.clients) c.terminate();
    await waitFor(() => authTokens.length === 2, 5000);
    stream.close();
    const before = authTokens.length;
    for (const c of wss.clients) c.terminate();
    await new Promise((r) => setTimeout(r, 1200));
    expect(authTokens.length).toBe(before); // no reconnect after close()
    await new Promise<void>((r) => wss.close(() => r()));
  });
});

async function waitFor(cond: () => boolean, ms = 3000): Promise<void> {
  const end = Date.now() + ms;
  while (!cond()) {
    if (Date.now() > end) throw new Error('waitFor timed out');
    await new Promise((r) => setTimeout(r, 25));
  }
}
