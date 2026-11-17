/**
 * Provisioning of the managed Python backend: find a suitable Python (>= 3.11), create a venv in the extension's global
 * storage and install the wheel bundled in the VSIX. Pure Node (no `vscode` import) so it is unit-testable.
 */
import { spawn as nodeSpawn, SpawnOptions, ChildProcess } from 'node:child_process';
import { readdirSync } from 'node:fs';
import * as path from 'node:path';

export const MIN_PYTHON: [number, number] = [3, 11];

export function venvPython(venvDir: string, platform: NodeJS.Platform = process.platform): string {
  return platform === 'win32' ? path.join(venvDir, 'Scripts', 'python.exe') : path.join(venvDir, 'bin', 'python');
}

export function findWheel(backendDir: string, list: (d: string) => string[] = (d) => readdirSync(d)): string | undefined {
  try {
    const wheels = list(backendDir)
      .filter((f) => /^patternvault-[\w.]+-.*\.whl$/.test(f))
      .sort();
    const last = wheels[wheels.length - 1];
    return last ? path.join(backendDir, last) : undefined;
  } catch {
    return undefined;
  }
}

export function pythonCandidates(configured: string, platform: NodeJS.Platform = process.platform): string[] {
  const names = platform === 'win32' ? ['py', 'python'] : ['python3.13', 'python3.12', 'python3.11', 'python3', 'python'];
  return [configured.trim(), ...names].filter(Boolean);
}

export function parsePythonVersion(output: string): [number, number] | undefined {
  const m = /(\d+)\.(\d+)/.exec(output);
  return m ? [Number(m[1]), Number(m[2])] : undefined;
}

export function versionOk(v: [number, number] | undefined): boolean {
  return !!v && (v[0] > MIN_PYTHON[0] || (v[0] === MIN_PYTHON[0] && v[1] >= MIN_PYTHON[1]));
}

export type Run = (cmd: string, args: string[]) => Promise<{ code: number | null; out: string }>;

export const defaultRun: Run = (cmd, args) =>
  new Promise((resolve) => {
    let out = '';
    let child: ChildProcess;
    try {
      child = nodeSpawn(cmd, args, { stdio: ['ignore', 'pipe', 'pipe'] });
    } catch {
      resolve({ code: null, out: '' });
      return;
    }
    child.stdout?.on('data', (b: Buffer) => (out += b.toString()));
    child.stderr?.on('data', (b: Buffer) => (out += b.toString()));
    child.on('error', () => resolve({ code: null, out }));
    child.on('close', (code) => resolve({ code, out }));
  });

/** First candidate that runs and reports Python >= 3.11. */
export async function detectPython(candidates: string[], run: Run = defaultRun): Promise<{ python: string; version: [number, number] } | undefined> {
  for (const c of candidates) {
    const r = await run(c, ['-c', 'import sys; print("%d.%d" % sys.version_info[:2])']);
    const v = r.code === 0 ? parsePythonVersion(r.out) : undefined;
    if (versionOk(v)) return { python: c, version: v as [number, number] };
  }
  return undefined;
}

export interface SetupStep {
  label: string;
  command: string;
  args: string[];
}

export function planSetup(systemPython: string, venvDir: string, wheel: string, platform: NodeJS.Platform = process.platform): SetupStep[] {
  const py = venvPython(venvDir, platform);
  return [
    { label: 'Creating a private Python environment', command: systemPython, args: ['-m', 'venv', '--clear', venvDir] },
    { label: 'Installing PatternVault and its dependencies (needs network once)', command: py, args: ['-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', wheel] },
    { label: 'Verifying the installation', command: py, args: ['-c', 'import app, sys; print("ok", sys.version.split()[0])'] },
  ];
}

export async function runSetup(steps: SetupStep[], log: (line: string) => void, spawnImpl: (c: string, a: string[], o: SpawnOptions) => ChildProcess = (c, a, o) => nodeSpawn(c, a, o)): Promise<void> {
  for (const step of steps) {
    log(`▶ ${step.label}`);
    log(`  $ ${step.command} ${step.args.join(' ')}`);
    const tail: string[] = [];
    const code = await new Promise<number | null>((resolve, reject) => {
      const child = spawnImpl(step.command, step.args, { stdio: ['ignore', 'pipe', 'pipe'] });
      const onData = (b: Buffer) => {
        for (const line of b.toString().split(/\r?\n/)) {
          if (!line.trim()) continue;
          log('  ' + line);
          tail.push(line);
          if (tail.length > 12) tail.shift();
        }
      };
      child.stdout?.on('data', onData);
      child.stderr?.on('data', onData);
      child.on('error', reject);
      child.on('close', resolve);
    });
    if (code !== 0) throw new Error(`"${step.label}" failed (exit ${code}):\n${tail.join('\n')}`);
  }
}
