/**
 * Typed client for the PatternVault backend (REST + WebSocket events).
 * Pure Node: no `vscode` import, so it is unit-testable against a real backend or a real `ws` server.
 */
import WebSocket from 'ws';

export class BackendError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly body?: unknown,
  ) {
    super(message);
    this.name = 'BackendError';
  }
}

export interface ClientOptions {
  baseUrl: string; // e.g. http://127.0.0.1:8765
  token: string;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

export type Mode = 'learning' | 'offline' | 'hybrid';
export type PermissionLevel = 'read_only' | 'assisted' | 'approved_execution' | 'restricted_autonomous';

export interface Dashboard {
  mode: Mode;
  auto_fallback: boolean;
  permission_level: PermissionLevel;
  counts: {
    projects: number;
    sessions: number;
    patterns: number;
    verified_patterns: number;
    generated_implementations: number;
  };
  agents_running: { agent: string; task_id: number | null; step: string; started_at: string }[];
  recent_tasks: { id: number; kind: string; request: string; status: string; verified: boolean; created_at: string }[];
  recent_sessions: { id: number; title: string; status: string; outcome: string; started_at: string }[];
  uptime_s: number;
}

export interface Project {
  id: number;
  name: string;
  root_path: string;
  languages: Record<string, unknown>;
  frameworks: unknown[];
  capture_enabled: boolean;
  share_knowledge: boolean;
}

export interface BackendEvent {
  seq: number;
  type: string;
  ts: string;
  data: Record<string, unknown>;
}

export interface ProviderInfo {
  key: string;
  display_name: string;
  kind: string;
  available: boolean;
  enabled: boolean;
  vscode_extension_ids: string[];
  capabilities: {
    captures_prompts: boolean;
    captures_responses: boolean;
    captures_tool_calls: boolean;
    captures_file_edits: boolean;
    mechanism: string;
    verified_against_real_install: boolean;
    notes: string;
  };
}

export interface ExternalSession {
  provider: string;
  external_id: string;
  title: string;
  cwd: string | null;
  started_at: string | null;
  message_count: number;
}

export interface StoreResult {
  pattern_id: number;
  action: 'created' | 'new_version' | 'duplicate';
  version: number;
}

export interface SessionSummary {
  id: number;
  project_id: number;
  title: string;
  status: string;
  outcome: string;
  prompts: number;
  responses: number;
  file_changes: number;
  extracted_pattern_ids: number[];
}

export const API_PREFIX = '/api/v1';

export class BackendClient {
  private readonly fetchImpl: typeof fetch;
  private readonly timeoutMs: number;

  constructor(private readonly opts: ClientOptions) {
    this.fetchImpl = opts.fetchImpl ?? fetch;
    this.timeoutMs = opts.timeoutMs ?? 30_000;
  }

  get baseUrl(): string {
    return this.opts.baseUrl;
  }

  async request<T>(method: string, path: string, body?: unknown, query?: Record<string, string | number | boolean | undefined>): Promise<T> {
    const url = new URL(API_PREFIX + path, this.opts.baseUrl);
    for (const [k, v] of Object.entries(query ?? {})) {
      if (v !== undefined) url.searchParams.set(k, String(v));
    }
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), this.timeoutMs);
    try {
      const res = await this.fetchImpl(url, {
        method,
        headers: {
          Authorization: `Bearer ${this.opts.token}`,
          ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
        },
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: ctrl.signal,
      });
      const text = await res.text();
      let parsed: unknown = undefined;
      if (text) {
        try {
          parsed = JSON.parse(text);
        } catch {
          parsed = text;
        }
      }
      if (!res.ok) {
        throw new BackendError(describeError(res.status, parsed), res.status, parsed);
      }
      return parsed as T;
    } catch (err) {
      if (err instanceof BackendError) throw err;
      if ((err as Error).name === 'AbortError') {
        throw new BackendError(`Request timed out after ${this.timeoutMs} ms: ${method} ${path}`, 0);
      }
      throw new BackendError(`Cannot reach backend: ${(err as Error).message}`, 0);
    } finally {
      clearTimeout(timer);
    }
  }

  health(): Promise<{ status: string; version: string; api_version: string }> {
    return this.request('GET', '/health');
  }
  dashboard(): Promise<Dashboard> {
    return this.request('GET', '/dashboard');
  }
  getConfig(): Promise<Record<string, unknown>> {
    return this.request('GET', '/config');
  }
  patchConfig(patch: Record<string, unknown>): Promise<Record<string, unknown>> {
    return this.request('PATCH', '/config', patch);
  }
  setMode(mode: Mode): Promise<{ mode: Mode; auto_fallback: boolean }> {
    return this.request('PUT', '/mode', { mode });
  }
  listProjects(): Promise<Project[]> {
    return this.request('GET', '/projects');
  }
  registerProject(rootPath: string, name?: string): Promise<Project> {
    return this.request('POST', '/projects', { root_path: rootPath, name });
  }
  analyzeProject(id: number): Promise<{ primary_language: string | null; frameworks: { name: string; version: string }[]; file_count: number }> {
    return this.request('POST', `/projects/${id}/analyze`);
  }
  providers(): Promise<ProviderInfo[]> {
    return this.request('GET', '/providers');
  }
  providerSessions(key: string, projectId: number): Promise<ExternalSession[]> {
    return this.request('GET', `/providers/${encodeURIComponent(key)}/sessions`, undefined, { project_id: projectId });
  }
  importSession(key: string, projectId: number, externalId: string, force = false): Promise<{ session_id: number; file_changes: number; patterns?: StoreResult[] }> {
    return this.request('POST', `/providers/${encodeURIComponent(key)}/import`, { project_id: projectId, external_id: externalId, force });
  }
  startSession(projectId: number, title: string, provider = 'manual'): Promise<SessionSummary> {
    return this.request('POST', '/sessions', { project_id: projectId, title, provider });
  }
  endSession(id: number, outcome: 'passed' | 'failed' | 'unknown' | undefined): Promise<{ session_id: number; changes: number; patterns?: StoreResult[] }> {
    return this.request('POST', `/sessions/${id}/end`, { outcome, extract: true });
  }
  listPatterns(query: Record<string, string | number | undefined> = {}): Promise<{ total: number; items: { id: number; title: string; status: string; category: string; confidence: number }[] }> {
    return this.request('GET', '/patterns', undefined, query);
  }
  auditVerify(): Promise<{ ok: boolean; checked: number; first_bad_id: number | null }> {
    return this.request('GET', '/audit/verify');
  }

  /** Open the live event stream. Reconnects with backoff until `close()` is called. */
  connectEvents(onEvent: (e: BackendEvent) => void, onState?: (connected: boolean) => void): EventStream {
    const wsUrl = new URL(API_PREFIX + '/ws', this.opts.baseUrl);
    wsUrl.protocol = wsUrl.protocol === 'https:' ? 'wss:' : 'ws:';
    return new EventStream(wsUrl.toString(), this.opts.token, onEvent, onState);
  }
}

function describeError(status: number, body: unknown): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === 'string') return `${status}: ${d}`;
    if (Array.isArray(d)) return `${status}: ${d.map((x) => (x && typeof x === 'object' && 'msg' in x ? String((x as { msg: unknown }).msg) : String(x))).join('; ')}`;
  }
  return `HTTP ${status}`;
}

export class EventStream {
  private ws: WebSocket | undefined;
  private closed = false;
  private attempt = 0;
  private timer: NodeJS.Timeout | undefined;

  constructor(
    private readonly url: string,
    private readonly token: string,
    private readonly onEvent: (e: BackendEvent) => void,
    private readonly onState?: (connected: boolean) => void,
  ) {
    this.open();
  }

  private open(): void {
    if (this.closed) return;
    const ws = new WebSocket(this.url);
    this.ws = ws;
    ws.on('open', () => ws.send(JSON.stringify({ type: 'auth', token: this.token })));
    ws.on('message', (data) => {
      try {
        const msg = JSON.parse(data.toString()) as BackendEvent & { type: string };
        if (msg.type === 'ready') {
          this.attempt = 0;
          this.onState?.(true);
        } else {
          this.onEvent(msg);
        }
      } catch {
        /* ignore malformed frames */
      }
    });
    const onGone = () => {
      this.onState?.(false);
      if (this.closed) return;
      const delay = Math.min(30_000, 500 * 2 ** this.attempt++);
      this.timer = setTimeout(() => this.open(), delay);
    };
    ws.on('close', onGone);
    ws.on('error', () => {
      /* 'close' follows; reconnect handled there */
    });
  }

  close(): void {
    this.closed = true;
    if (this.timer) clearTimeout(this.timer);
    this.ws?.removeAllListeners();
    this.ws?.on('error', () => undefined);
    this.ws?.close();
  }
}
