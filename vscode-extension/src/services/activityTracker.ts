/**
 * Counts file changes during a capture window. Stores PATHS ONLY (never contents) and drops obviously
 * irrelevant/sensitive paths client-side. The backend performs the authoritative, content-aware diff.
 */
const IGNORED_DIRS = new Set(['.git', 'node_modules', '.venv', 'venv', '__pycache__', 'dist', 'build', 'out', '.next', 'target', '.idea', '.mypy_cache', '.pytest_cache', '.ruff_cache', 'coverage']);
const SENSITIVE = [/^\.env(\..*)?$/i, /\.(pem|key|p12|pfx|jks|keystore)$/i, /^id_(rsa|dsa|ecdsa|ed25519)/i, /^\.npmrc$/i, /^\.netrc$/i];

export function isTrackablePath(relPath: string): boolean {
  const parts = relPath.replace(/\\/g, '/').split('/').filter(Boolean);
  if (parts.length === 0) return false;
  if (parts.slice(0, -1).some((p) => IGNORED_DIRS.has(p))) return false;
  const base = parts[parts.length - 1] as string;
  return !SENSITIVE.some((rx) => rx.test(base));
}

export class ActivityTracker {
  private readonly paths = new Set<string>();
  private active = false;

  start(): void {
    this.paths.clear();
    this.active = true;
  }

  stop(): number {
    this.active = false;
    return this.paths.size;
  }

  /** Record a change. Returns true when it was counted. */
  record(relPath: string): boolean {
    if (!this.active || !isTrackablePath(relPath)) return false;
    this.paths.add(relPath.replace(/\\/g, '/'));
    return true;
  }

  get count(): number {
    return this.paths.size;
  }

  get isActive(): boolean {
    return this.active;
  }
}
