/**
 * Test plan: managed-backend provisioning (pure parts) - wheel discovery, interpreter detection/version gate,
 * venv python path per platform, the exact install plan, and sequential execution with failure reporting
 * using REAL child processes (node standing in for python/pip).
 */
import { describe, expect, it } from 'vitest';
import { detectPython, findWheel, parsePythonVersion, planSetup, pythonCandidates, runSetup, versionOk, venvPython } from '../../src/services/backendSetup';
import { resolvePython } from '../../src/services/backendProcess';

describe('discovery helpers', () => {
  it('finds the newest bundled wheel only', () => {
    expect(findWheel('/b', () => ['readme.txt', 'patternvault-0.1.0-py3-none-any.whl', 'patternvault-0.2.0-py3-none-any.whl', 'other-1.0.whl'])).toBe('/b/patternvault-0.2.0-py3-none-any.whl');
    expect(findWheel('/b', () => ['x.txt'])).toBeUndefined();
    expect(findWheel('/b', () => { throw new Error('ENOENT'); })).toBeUndefined();
  });
  it('venv python path per platform', () => {
    expect(venvPython('/g/venv', 'linux')).toBe('/g/venv/bin/python');
    expect(venvPython('C:\\g\\venv', 'win32')).toMatch(/Scripts[\\/]python\.exe$/);
  });
  it('version parsing and the 3.11 gate', () => {
    expect(parsePythonVersion('3.13\n')).toEqual([3, 13]);
    expect(parsePythonVersion('Python 2.7.18')).toEqual([2, 7]);
    expect(parsePythonVersion('nope')).toBeUndefined();
    expect(versionOk([3, 11])).toBe(true);
    expect(versionOk([3, 10])).toBe(false);
    expect(versionOk([4, 0])).toBe(true);
    expect(versionOk(undefined)).toBe(false);
  });
  it('candidate order prefers the configured interpreter then newest python3.x', () => {
    expect(pythonCandidates('/opt/py', 'linux')).toEqual(['/opt/py', 'python3.13', 'python3.12', 'python3.11', 'python3', 'python']);
    expect(pythonCandidates('  ', 'linux')[0]).toBe('python3.13');
  });
  it('detectPython skips interpreters that are missing or too old', async () => {
    const fake = async (cmd: string) => (cmd === 'python3.13' ? { code: null, out: '' } : cmd === 'python3.12' ? { code: 0, out: '3.9\n' } : cmd === 'python3.11' ? { code: 0, out: '3.11\n' } : { code: 1, out: '' });
    expect(await detectPython(['python3.13', 'python3.12', 'python3.11'], fake)).toEqual({ python: 'python3.11', version: [3, 11] });
    expect(await detectPython(['python3.13', 'python3.12'], fake)).toBeUndefined();
  });
});

describe('resolvePython precedence with a managed venv', () => {
  it('configured > managed > workspace .venv > default', () => {
    const exists = (p: string) => p === '/g/venv/bin/python' || p === '/w/.venv/bin/python';
    expect(resolvePython({ configured: '/x/py', managedPython: '/g/venv/bin/python', searchRoots: ['/w'], exists, platform: 'linux' })).toBe('/x/py');
    expect(resolvePython({ configured: '', managedPython: '/g/venv/bin/python', searchRoots: ['/w'], exists, platform: 'linux' })).toBe('/g/venv/bin/python');
    expect(resolvePython({ configured: '', managedPython: '/missing', searchRoots: ['/w'], exists, platform: 'linux' })).toBe('/w/.venv/bin/python');
    expect(resolvePython({ configured: '', searchRoots: [], exists: () => false, platform: 'linux' })).toBe('python3');
  });
});

describe('install plan and runner', () => {
  it('plans venv → pip install of the bundled wheel → import check, with no shell involved', () => {
    const steps = planSetup('/usr/bin/python3.13', '/g/venv', '/e/backend/patternvault-0.1.0-py3-none-any.whl', 'linux');
    expect(steps.map((s) => [s.command, s.args[0]])).toEqual([['/usr/bin/python3.13', '-m'], ['/g/venv/bin/python', '-m'], ['/g/venv/bin/python', '-c']]);
    expect(steps[1]?.args).toContain('/e/backend/patternvault-0.1.0-py3-none-any.whl');
    expect(steps[1]?.args).toContain('--disable-pip-version-check');
  });
  it('runs steps in order with real processes and logs output', async () => {
    const logs: string[] = [];
    await runSetup([{ label: 'one', command: process.execPath, args: ['-e', "console.log('hello-1')"] }, { label: 'two', command: process.execPath, args: ['-e', "console.error('hello-2')"] }], (l) => logs.push(l));
    expect(logs.filter((l) => l.startsWith('▶'))).toEqual(['▶ one', '▶ two']);
    expect(logs.join('\n')).toContain('hello-1');
    expect(logs.join('\n')).toContain('hello-2');
  });
  it('stops at the first failing step and reports the tail of its output', async () => {
    const logs: string[] = [];
    await expect(runSetup([
      { label: 'ok', command: process.execPath, args: ['-e', '0'] },
      { label: 'boom', command: process.execPath, args: ['-e', "console.error('pip exploded'); process.exit(3)"] },
      { label: 'never', command: process.execPath, args: ['-e', "console.log('should-not-run')"] },
    ], (l) => logs.push(l))).rejects.toThrow(/"boom" failed \(exit 3\)[\s\S]*pip exploded/);
    expect(logs.join('\n')).not.toContain('should-not-run');
  });
  it('rejects when the command cannot be spawned', async () => {
    await expect(runSetup([{ label: 'x', command: '/definitely/missing/python', args: [] }], () => undefined)).rejects.toThrow();
  });
});
