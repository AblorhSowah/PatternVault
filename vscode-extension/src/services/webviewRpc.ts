/**
 * Host-side RPC for the Workbench webview. The webview is treated as UNTRUSTED: it renders text that originates
 * from captured AI sessions. Therefore it can only call an explicit allow-list of backend routes, dangerous
 * operations additionally require a host-side modal confirmation, and the API token never leaves the host.
 */
import { BackendClient, BackendError } from './backendClient';

export interface ApiMessage {
  type: 'api';
  id: number;
  method: string;
  path: string;
  query?: Record<string, string | number | boolean | undefined>;
  body?: unknown;
}

type Rule = { method: string; pattern: RegExp };
const ID = '\\d{1,9}';
const KEY = '[a-z0-9][a-z0-9._-]{0,63}';
const ALLOWED: Rule[] = [
  ['GET', '/dashboard'], ['GET', '/mode'], ['PUT', '/mode'], ['GET', '/config'], ['PATCH', '/config'],
  ['GET', '/projects'], ['POST', `/projects/${ID}/analyze`], ['GET', '/providers'], ['GET', `/providers/${KEY}/sessions`],
  ['POST', `/providers/${KEY}/import`], ['GET', '/sessions'], ['GET', `/sessions/${ID}`], ['POST', '/sessions'],
  ['POST', `/sessions/${ID}/end`], ['GET', '/patterns'], ['GET', '/patterns/facets'], ['GET', `/patterns/${ID}`],
  ['GET', `/patterns/${ID}/versions`], ['PATCH', `/patterns/${ID}`], ['POST', `/patterns/${ID}/approve`],
  ['POST', `/patterns/${ID}/deprecate`], ['POST', `/patterns/${ID}/restore`], ['POST', `/patterns/${ID}/feedback`],
  ['DELETE', `/patterns/${ID}`], ['POST', '/search'], ['POST', '/context'], ['GET', '/knowledge/stats'],
  ['POST', '/knowledge/reindex'], ['POST', '/knowledge/quality'], ['GET', '/knowledge/export'], ['GET', '/audit/verify'],
  ...EXTRA_RULES(),
].map(([method, p]) => ({ method: method as string, pattern: new RegExp(`^${p}$`) }));

// Later phases register additional routes here (models, generation, agents, testing, approvals).
function EXTRA_RULES(): [string, string][] {
  return [
    ['GET', '/models'], ['GET', '/models/status'], ['POST', '/models/select'], ['POST', '/models/pull'],
    ['GET', '/storage'], ['POST', '/storage/gc'], ['GET', '/backups'], ['POST', '/backups'], ['POST', '/backups/verify'], ['POST', '/backups/restore'], ['DELETE', '/backups/[\\w.-]+'], ['GET', '/audit'], ['GET', '/knowledge/stats'], ['POST', '/generate'], ['POST', '/tasks'], ['POST', '/testing/run'], ['POST', `/approvals/${ID}/decide`], ['POST', `/recovery-points/${ID}/restore`], ['GET', '/tasks/' + ID + '/report'], ['POST', '/replay/plan'], ['POST', '/compare'], ['GET', '/implementations'], ['GET', `/implementations/${ID}`], ['GET', '/modes/availability'], ['GET', '/system/resources'], ['POST', '/models/test'], ['GET', '/tasks'], ['GET', `/tasks/${ID}`],
    ['POST', `/tasks/${ID}/cancel`], ['GET', '/agents'], ['PATCH', `/agents/${KEY}`], ['GET', '/testing/sessions'], ['GET', '/testing/summary'],
    ['GET', `/testing/sessions/${ID}`], ['POST', '/testing/run'], ['GET', '/approvals'], ['GET', '/recovery-points'],
  ];
}

export const ALLOWED_COMMANDS = new Set(['patternvault.setProviderKey', 'patternvault.startCapture', 'patternvault.endCapture', 'patternvault.importSessions', 'patternvault.openWorkbench', 'patternvault.analyzeProject']);
const HIGH_RISK_PERMISSIONS = new Set(['approved_execution', 'restricted_autonomous']);

export function isAllowed(method: string, path: string): boolean {
  if (!path.startsWith('/') || path.includes('..') || path.includes('//') || path.includes('?') || path.includes('#')) return false;
  return ALLOWED.some((r) => r.method === method.toUpperCase() && r.pattern.test(path));
}

/** Returns a human-readable prompt if the host must get explicit user confirmation first. */
export function confirmationFor(method: string, path: string, body: unknown): string | undefined {
  const m = method.toUpperCase();
  if (m === 'DELETE' && path.startsWith('/patterns/')) return 'Permanently delete this pattern and its version history?';
  const b0 = (body && typeof body === 'object' ? body : {}) as Record<string, unknown>;
  if (m === 'POST' && path === '/tasks') {
    return `Start a "${String(b0.kind ?? '?')}" task: “${String(b0.request ?? '').slice(0, 120)}”? Agents may read, change files and run test commands as allowed by your permission level.`;
  }
  if (m === 'POST' && path === '/backups/restore') return 'Restore this backup? Your current knowledge base is replaced (an automatic safety copy is made first).';
  if (m === 'DELETE' && path.startsWith('/backups/')) return 'Permanently delete this backup file?';
  if (m === 'POST' && path === '/testing/run') return 'Run the selected testing agents? This executes the project’s own test commands.';
  if (m === 'POST' && /^\/approvals\/\d+\/decide$/.test(path) && b0.approve === true) return 'Approve this agent action? Its details are shown in the Agent Control Centre.';
  if (m === 'POST' && /^\/recovery-points\/\d+\/restore$/.test(path)) return 'Restore files from this recovery point? Current content of those files will be replaced.';
  if (m === 'PATCH' && path === '/config' && body && typeof body === 'object') {
    const b = body as Record<string, unknown>;
    if (typeof b.permission_level === 'string' && HIGH_RISK_PERMISSIONS.has(b.permission_level)) {
      return `Allow agents to act with "${b.permission_level.replace(/_/g, ' ')}" permissions? Destructive commands stay blocked and recovery points are always taken.`;
    }
    const cloud = b.cloud as { consent?: Record<string, boolean> } | undefined;
    if (cloud?.consent && Object.values(cloud.consent).some(Boolean)) return 'Allow sending prompts and code to this external service?';
    const lm = b.local_model as { allow_remote_host?: boolean } | undefined;
    if (lm?.allow_remote_host) return 'Allow sending code to a non-local model host?';
  }
  return undefined;
}

export interface RpcDeps {
  getClient: () => BackendClient | undefined;
  confirm: (message: string) => Promise<boolean>;
  saveJson: (suggestedName: string, data: unknown) => Promise<boolean>;
  runCommand: (command: string) => Promise<void>;
}

export type RpcMessage =
  | ApiMessage
  | { type: 'confirm'; id: number; message: string }
  | { type: 'saveJson'; id: number; suggestedName: string; data: unknown }
  | { type: 'command'; id: number; command: string };

export type RpcResponse =
  | { type: 'apiResult'; id: number; ok: boolean; status: number; data?: unknown; error?: string }
  | { type: 'confirmResult'; id: number; ok: boolean }
  | { type: 'saveResult'; id: number; ok: boolean }
  | { type: 'commandResult'; id: number; ok: boolean; error?: string };

export async function handleRpc(msg: RpcMessage, deps: RpcDeps): Promise<RpcResponse | undefined> {
  switch (msg.type) {
    case 'api': {
      const method = String(msg.method || '').toUpperCase();
      if (!isAllowed(method, String(msg.path))) return { type: 'apiResult', id: msg.id, ok: false, status: 403, error: `route not permitted from the webview: ${method} ${msg.path}` };
      const prompt = confirmationFor(method, msg.path, msg.body);
      if (prompt && !(await deps.confirm(prompt))) return { type: 'apiResult', id: msg.id, ok: false, status: 499, error: 'cancelled by user' };
      const client = deps.getClient();
      if (!client) return { type: 'apiResult', id: msg.id, ok: false, status: 503, error: 'backend is not running' };
      try {
        const data = await client.request(method, msg.path, msg.body, msg.query);
        return { type: 'apiResult', id: msg.id, ok: true, status: 200, data };
      } catch (err) {
        const e = err as BackendError;
        return { type: 'apiResult', id: msg.id, ok: false, status: e.status ?? 500, error: e.message };
      }
    }
    case 'confirm':
      return { type: 'confirmResult', id: msg.id, ok: await deps.confirm(String(msg.message).slice(0, 500)) };
    case 'saveJson':
      return { type: 'saveResult', id: msg.id, ok: await deps.saveJson(String(msg.suggestedName).replace(/[^\w.-]/g, '_').slice(0, 80), msg.data) };
    case 'command': {
      if (!ALLOWED_COMMANDS.has(msg.command)) return { type: 'commandResult', id: msg.id, ok: false, error: 'command not permitted' };
      try {
        await deps.runCommand(msg.command);
        return { type: 'commandResult', id: msg.id, ok: true };
      } catch (err) {
        return { type: 'commandResult', id: msg.id, ok: false, error: (err as Error).message };
      }
    }
    default:
      return undefined;
  }
}
