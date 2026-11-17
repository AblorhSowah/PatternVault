/**
 * Provider API keys live ONLY in VS Code SecretStorage (OS keychain backed). Nothing here writes keys to
 * settings, files or the backend's config.
 */

export interface SecretStore {
  get(key: string): Thenable<string | undefined>;
  store(key: string, value: string): Thenable<void>;
  delete(key: string): Thenable<void>;
}

export const KEY_PREFIX = 'patternvault.providerKey.';
const PROVIDER_RE = /^[a-z0-9][a-z0-9._-]{0,63}$/;

export function secretKeyFor(provider: string): string {
  const p = provider.trim().toLowerCase();
  if (!PROVIDER_RE.test(p)) throw new Error('Provider id must be 1-64 chars: a-z, 0-9, ".", "_" or "-"');
  return KEY_PREFIX + p;
}

export class ProviderKeyStore {
  constructor(private readonly store: SecretStore) {}

  async set(provider: string, apiKey: string): Promise<void> {
    if (!apiKey.trim()) throw new Error('API key is empty');
    await this.store.store(secretKeyFor(provider), apiKey.trim());
  }

  get(provider: string): Thenable<string | undefined> {
    return this.store.get(secretKeyFor(provider));
  }

  async has(provider: string): Promise<boolean> {
    return Boolean(await this.get(provider));
  }

  clear(provider: string): Thenable<void> {
    return this.store.delete(secretKeyFor(provider));
  }
}
