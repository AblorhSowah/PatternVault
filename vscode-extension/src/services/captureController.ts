/** Capture-window state machine (VS Code-free; storage and client are injected). */
import type { BackendClient, StoreResult } from './backendClient';
import { ActivityTracker } from './activityTracker';

export interface KeyValueStore {
  get<T>(key: string): T | undefined;
  update(key: string, value: unknown): Thenable<void>;
}

const KEY = 'patternvault.activeCapture';
interface ActiveCapture {
  sessionId: number;
  projectId: number;
  title: string;
}

export class CaptureController {
  readonly tracker = new ActivityTracker();
  private active: ActiveCapture | undefined;

  constructor(
    private readonly getClient: () => BackendClient | undefined,
    private readonly store: KeyValueStore,
  ) {
    this.active = store.get<ActiveCapture>(KEY);
    if (this.active) this.tracker.start(); // resumed after a reload; count restarts, the backend baseline persists
  }

  get current(): ActiveCapture | undefined {
    return this.active;
  }

  private client(): BackendClient {
    const c = this.getClient();
    if (!c) throw new Error('backend is not running');
    return c;
  }

  async start(projectId: number, title: string): Promise<ActiveCapture> {
    if (this.active) throw new Error('a capture session is already active — end it first');
    const s = await this.client().startSession(projectId, title);
    this.active = { sessionId: s.id, projectId, title: s.title };
    await this.store.update(KEY, this.active);
    this.tracker.start();
    return this.active;
  }

  async end(outcome: 'passed' | 'failed' | 'unknown' | undefined): Promise<{ changes: number; patterns: StoreResult[]; changedFilesSeen: number }> {
    if (!this.active) throw new Error('no active capture session');
    const res = await this.client().endSession(this.active.sessionId, outcome);
    const seen = this.tracker.stop();
    this.active = undefined;
    await this.store.update(KEY, undefined);
    return { changes: res.changes, patterns: res.patterns ?? [], changedFilesSeen: seen };
  }

  /** Forget a stale active session (e.g. the backend DB was reset). */
  async discard(): Promise<void> {
    this.tracker.stop();
    this.active = undefined;
    await this.store.update(KEY, undefined);
  }
}
