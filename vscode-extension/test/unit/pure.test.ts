/**
 * Test plan (pure logic): settings reader, SecretStorage key handling, status presentation.
 */
import { describe, expect, it } from 'vitest';
import { readSettings } from '../../src/configuration/settings';
import { KEY_PREFIX, ProviderKeyStore, secretKeyFor, SecretStore } from '../../src/services/secrets';
import { statusBarModel } from '../../src/services/statusModel';
import type { Dashboard } from '../../src/services/backendClient';

const reader = (values: Record<string, unknown>) => ({
  get: <T>(key: string, d: T): T => (key in values ? (values[key] as T) : d),
});

describe('settings', () => {
  it('uses defaults', () => {
    expect(readSettings(reader({}))).toEqual({ autoStart: true, pythonPath: '', home: '', startupTimeoutMs: 30000 });
  });
  it('rejects nonsense timeouts', () => {
    expect(readSettings(reader({ 'backend.startupTimeoutMs': 5 })).startupTimeoutMs).toBe(30000);
    expect(readSettings(reader({ 'backend.startupTimeoutMs': 'abc' })).startupTimeoutMs).toBe(30000);
    expect(readSettings(reader({ 'backend.startupTimeoutMs': 12000 })).startupTimeoutMs).toBe(12000);
  });
});

class MemStore implements SecretStore {
  data = new Map<string, string>();
  async get(k: string) { return this.data.get(k); }
  async store(k: string, v: string) { this.data.set(k, v); }
  async delete(k: string) { this.data.delete(k); }
}

describe('ProviderKeyStore', () => {
  it('namespaces keys and round-trips', async () => {
    const mem = new MemStore();
    const ks = new ProviderKeyStore(mem);
    await ks.set('OpenAI-Compatible', '  sk-abc  ');
    expect([...mem.data.keys()]).toEqual([KEY_PREFIX + 'openai-compatible']);
    expect(await ks.get('openai-compatible')).toBe('sk-abc');
    expect(await ks.has('openai-compatible')).toBe(true);
    await ks.clear('openai-compatible');
    expect(await ks.has('openai-compatible')).toBe(false);
  });
  it('rejects empty keys and weird provider ids', async () => {
    const ks = new ProviderKeyStore(new MemStore());
    await expect(ks.set('x', '   ')).rejects.toThrow();
    expect(() => secretKeyFor('../etc')).toThrow();
    expect(() => secretKeyFor('')).toThrow();
    expect(() => secretKeyFor('a b')).toThrow();
  });
});

const dash = (over: Partial<Dashboard> = {}): Dashboard => ({
  mode: 'offline', auto_fallback: true, permission_level: 'assisted',
  counts: { projects: 1, sessions: 2, patterns: 5, verified_patterns: 2, generated_implementations: 0 },
  agents_running: [], recent_tasks: [], recent_sessions: [], uptime_s: 1, ...over,
});

describe('statusBarModel', () => {
  it('shows mode and counts when running', () => {
    const m = statusBarModel('running', dash());
    expect(m.text).toContain('Offline');
    expect(m.tooltip).toContain('5 patterns (2 verified)');
    expect(m.command).toBe('patternvault.switchMode');
  });
  it('failed state is a warning and links to the log', () => {
    const m = statusBarModel('failed', undefined, 'boom');
    expect(m.warning).toBe(true);
    expect(m.tooltip).toContain('boom');
    expect(m.command).toBe('patternvault.showLog');
  });
  it('stopped offers start; starting shows spinner', () => {
    expect(statusBarModel('stopped', undefined).command).toBe('patternvault.startBackend');
    expect(statusBarModel('starting', undefined).text).toContain('sync~spin');
  });
});
