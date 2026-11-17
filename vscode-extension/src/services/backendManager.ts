/** Owns the backend lifecycle for the extension host: settings → launch plan → process → client + events. */
import * as vscode from 'vscode';
import * as path from 'node:path';
import { BackendClient, BackendEvent, EventStream } from './backendClient';
import { BackendProcess, BackendStatus, buildLaunchPlan, findFreePort, generateToken, resolvePython } from './backendProcess';
import { detectPython, findWheel, planSetup, pythonCandidates, runSetup, venvPython } from './backendSetup';
import { readSettings } from '../configuration/settings';

export class BackendManager implements vscode.Disposable {
  readonly process: BackendProcess;
  client: BackendClient | undefined;
  private events: EventStream | undefined;
  private readonly statusEmitter = new vscode.EventEmitter<BackendStatus>();
  private readonly eventEmitter = new vscode.EventEmitter<BackendEvent>();
  readonly onDidChangeStatus = this.statusEmitter.event;
  readonly onDidReceiveEvent = this.eventEmitter.event;
  /** Interpreter used for the most recent start (exposed for diagnostics and tests). */
  lastPython: string | undefined;

  get venvDir(): string {
    return path.join(this.context.globalStorageUri.fsPath, 'venv');
  }

  get managedPython(): string {
    return venvPython(this.venvDir);
  }

  /** Create the managed environment from the wheel bundled in the VSIX (needs network for dependencies, once). */
  async setup(progress: (msg: string) => void): Promise<void> {
    const wheel = findWheel(path.join(this.context.extensionPath, 'backend'));
    if (!wheel) {
      throw new Error('This build of the extension has no bundled backend wheel. Install `patternvault` into a Python 3.11+ environment and set "patternvault.backend.pythonPath".');
    }
    const configured = vscode.workspace.getConfiguration('patternvault').get<string>('backend.systemPython', '');
    const found = await detectPython(pythonCandidates(configured));
    if (!found) throw new Error('Python 3.11 or newer was not found. Install it, or set "patternvault.backend.systemPython".');
    this.output.appendLine(`using ${found.python} (Python ${found.version.join('.')}) and ${path.basename(wheel)}`);
    await this.stop();
    await runSetup(planSetup(found.python, this.venvDir, wheel), (l) => {
      this.output.appendLine(l);
      if (l.startsWith('▶')) progress(l.slice(2));
    });
  }

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly output: vscode.OutputChannel,
  ) {
    this.process = new BackendProcess({ log: (l) => this.output.appendLine(l) });
    this.process.onStatus((s) => this.statusEmitter.fire(s));
  }

  get status(): BackendStatus {
    return this.process.status;
  }

  get lastError(): string | undefined {
    return this.process.lastError;
  }

  async start(): Promise<void> {
    const settings = readSettings(vscode.workspace.getConfiguration('patternvault'));
    const roots = [
      ...(vscode.workspace.workspaceFolders ?? []).map((f) => f.uri.fsPath),
      path.resolve(this.context.extensionPath, '..'), // dev checkout: <repo>/vscode-extension → <repo>
    ];
    const python = resolvePython({ configured: settings.pythonPath, managedPython: this.managedPython, searchRoots: roots });
    this.lastPython = python;
    const port = await findFreePort();
    const token = generateToken();
    const plan = buildLaunchPlan(python, port, token, settings.home);
    this.output.appendLine(`starting backend: ${python} -m app --port ${port}`);
    const running = await this.process.start(plan, port, settings.startupTimeoutMs);
    this.client = new BackendClient({ baseUrl: running.baseUrl, token: running.token });
    this.events?.close();
    this.events = this.client.connectEvents((e) => this.eventEmitter.fire(e));
  }

  async stop(): Promise<void> {
    this.events?.close();
    this.events = undefined;
    await this.process.stop();
    this.client = undefined;
  }

  async restart(): Promise<void> {
    await this.stop();
    await this.start();
  }

  dispose(): void {
    void this.stop();
    this.statusEmitter.dispose();
    this.eventEmitter.dispose();
  }
}
