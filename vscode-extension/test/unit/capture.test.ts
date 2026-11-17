/**
 * Test plan: activity tracker (paths only, sensitive/ignored paths never counted), assistant detection from
 * honest backend capabilities, and the capture controller state machine (resume, double-start, errors).
 */
import { describe, expect, it } from 'vitest';
import { detectAssistants } from '../../src/providers/assistantDetector';
import type { BackendClient, ProviderInfo } from '../../src/services/backendClient';
import { ActivityTracker, isTrackablePath } from '../../src/services/activityTracker';
import { CaptureController, KeyValueStore } from '../../src/services/captureController';

describe('isTrackablePath', () => {
  it.each(['src/a.ts', 'app/Http/Controllers/Auth.php', 'README.md'])('tracks %s', (p) => expect(isTrackablePath(p)).toBe(true));
  it.each(['.env', '.env.production', 'config/.env', 'certs/server.pem', 'id_rsa', 'a/id_ed25519.pub', '.npmrc', 'node_modules/x/i.js', '.git/HEAD', 'dist/app.js', 'a/__pycache__/m.pyc', ''])(
    'ignores %s',
    (p) => expect(isTrackablePath(p)).toBe(false),
  );
  it('normalises windows separators', () => {
    expect(isTrackablePath('src\\a.ts')).toBe(true);
    expect(isTrackablePath('node_modules\\x\\i.js')).toBe(false);
  });
});

describe('ActivityTracker', () => {
  it('only counts while active, dedupes, and stores no contents', () => {
    const t = new ActivityTracker();
    expect(t.record('a.ts')).toBe(false); // inactive
    t.start();
    expect(t.record('a.ts')).toBe(true);
    t.record('a.ts');
    t.record('b.ts');
    t.record('.env');
    expect(t.count).toBe(2);
    expect(t.stop()).toBe(2);
    expect(t.isActive).toBe(false);
    t.start();
    expect(t.count).toBe(0);
  });
});

const prov = (over: Partial<ProviderInfo> & { key: string }): ProviderInfo => ({
  display_name: over.key, kind: 'cloud_assistant', available: false, enabled: true, vscode_extension_ids: [],
  capabilities: { captures_prompts: false, captures_responses: false, captures_tool_calls: false, captures_file_edits: false, mechanism: 'm', verified_against_real_install: false, notes: '' },
  ...over,
});

describe('detectAssistants', () => {
  it('reports install state and honest capture mode', () => {
    const providers = [
      prov({ key: 'claude-code', vscode_extension_ids: ['anthropic.claude-code'], capabilities: { captures_prompts: true, captures_responses: true, captures_tool_calls: true, captures_file_edits: true, mechanism: 'reads local logs', verified_against_real_install: true, notes: '' } }),
      prov({ key: 'continue', vscode_extension_ids: ['continue.continue'], capabilities: { captures_prompts: true, captures_responses: true, captures_tool_calls: false, captures_file_edits: false, mechanism: 'reads sessions', verified_against_real_install: false, notes: '' } }),
      prov({ key: 'github-copilot', vscode_extension_ids: ['github.copilot-chat'] }),
      prov({ key: 'manual' }),
    ];
    const res = detectAssistants(['Anthropic.Claude-Code', 'GitHub.copilot-chat'], providers);
    expect(res.map((r) => r.key)).toEqual(['claude-code', 'continue', 'github-copilot']); // manual hidden
    expect(res[0]).toMatchObject({ installed: true, capture: 'automatic-import' });
    expect(res[1]).toMatchObject({ installed: false, capture: 'automatic-import' });
    expect(res[1]?.summary).toContain('unverified');
    expect(res[2]).toMatchObject({ installed: true, capture: 'workspace-and-manual' });
    expect(res[2]?.summary).toContain('no capture API');
  });
});

class MemKv implements KeyValueStore {
  data = new Map<string, unknown>();
  get<T>(k: string) { return this.data.get(k) as T | undefined; }
  async update(k: string, v: unknown) { if (v === undefined) this.data.delete(k); else this.data.set(k, v); }
}

function fakeClient() {
  const calls: string[] = [];
  const client = {
    startSession: async (pid: number, title: string) => { calls.push(`start:${pid}`); return { id: 11, project_id: pid, title, status: 'active', outcome: 'unknown', prompts: 0, responses: 0, file_changes: 0, extracted_pattern_ids: [] }; },
    endSession: async (id: number, outcome?: string) => { calls.push(`end:${id}:${outcome}`); return { session_id: id, changes: 3, patterns: [{ pattern_id: 1, action: 'created' as const, version: 1 }] }; },
  } as unknown as BackendClient;
  return { client, calls };
}

describe('CaptureController', () => {
  it('start → record → end lifecycle persists and clears state', async () => {
    const kv = new MemKv();
    const { client, calls } = fakeClient();
    const c = new CaptureController(() => client, kv);
    await c.start(5, 'auth');
    expect(c.current).toMatchObject({ sessionId: 11, projectId: 5 });
    c.tracker.record('a.ts');
    await expect(c.start(5, 'again')).rejects.toThrow(/already active/);
    const res = await c.end('passed');
    expect(res).toMatchObject({ changes: 3, changedFilesSeen: 1 });
    expect(c.current).toBeUndefined();
    expect(kv.data.size).toBe(0);
    expect(calls).toEqual(['start:5', 'end:11:passed']);
    await expect(c.end('passed')).rejects.toThrow(/no active/);
  });

  it('resumes an active session after a reload and can discard it', async () => {
    const kv = new MemKv();
    const { client } = fakeClient();
    await new CaptureController(() => client, kv).start(2, 't');
    const resumed = new CaptureController(() => client, kv);
    expect(resumed.current?.sessionId).toBe(11);
    expect(resumed.tracker.isActive).toBe(true);
    await resumed.discard();
    expect(resumed.current).toBeUndefined();
    expect(kv.data.size).toBe(0);
  });

  it('fails clearly when the backend is down', async () => {
    const c = new CaptureController(() => undefined, new MemKv());
    await expect(c.start(1, 't')).rejects.toThrow(/backend is not running/);
  });
});
