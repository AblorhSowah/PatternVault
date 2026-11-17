/**
 * Test plan: chat (host side + pure webview logic)
 * - Markdown subset: paragraphs/lists/headings/code/bold/citations; a half-streamed code fence still parses; nothing becomes HTML or a link
 * - the host RPC allow-list admits exactly the chat routes (and none that could write files); deletes need host confirmation
 * - copy / open-in-editor RPC: size and language validation, never touches existing files (the dep only opens an UNTITLED doc)
 * - "Ask about selection": credential-looking files are refused; fences cannot be broken out of; long selections are capped
 */
import { pathToFileURL } from 'node:url';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { buildAskPrompt, isSensitivePath, MAX_SELECTION_CHARS } from '../../src/services/askPrompt';
import { confirmationFor, handleRpc, isAllowed, RpcDeps } from '../../src/services/webviewRpc';

type Tok = { t: string; v?: string; id?: number };
type Block = { type: string; level?: number; inline?: Tok[]; items?: Tok[][]; lang?: string; text?: string; closed?: boolean };
type Md = { parseMarkdown: (s: string) => Block[]; parseInline: (s: string) => Tok[]; citedIds: (s: string) => number[] };

async function md(): Promise<Md> {
  const file = path.resolve(__dirname, '..', '..', 'media', 'webview', 'lib', 'markdown.js');
  return (await import(pathToFileURL(file).href)) as Md;
}

describe('markdown subset', () => {
  it('parses paragraphs, headings, lists, bold, inline code and citations', async () => {
    const { parseMarkdown } = await md();
    const blocks = parseMarkdown('# Title\n\nUse **JWT** with `PyJWT` [#12].\nSecond line.\n\n- one\n- two\n\n1. first\n2. second');
    expect(blocks.map((b) => b.type)).toEqual(['h', 'p', 'ul', 'ol']);
    expect(blocks[0]).toMatchObject({ level: 1 });
    expect(blocks[1]?.inline).toEqual([
      { t: 'text', v: 'Use ' },
      { t: 'strong', v: 'JWT' },
      { t: 'text', v: ' with ' },
      { t: 'code', v: 'PyJWT' },
      { t: 'text', v: ' ' },
      { t: 'cite', id: 12 },
      { t: 'text', v: '.\nSecond line.' },
    ]);
    expect(blocks[2]?.items).toHaveLength(2);
    expect(blocks[3]?.items).toHaveLength(2);
  });

  it('keeps fenced code verbatim (no inline parsing inside), with the language', async () => {
    const { parseMarkdown } = await md();
    const [b] = parseMarkdown('```Python\nx = "**not bold** [#9]"\n```');
    expect(b).toEqual({ type: 'code', lang: 'python', text: 'x = "**not bold** [#9]"', closed: true });
  });

  it('renders a half-streamed code fence as an open code block instead of garbage', async () => {
    const { parseMarkdown } = await md();
    const blocks = parseMarkdown('Here you go:\n\n```ts\nconst a = 1;\nconst b');
    expect(blocks.map((b) => b.type)).toEqual(['p', 'code']);
    expect(blocks[1]).toMatchObject({ lang: 'ts', closed: false, text: 'const a = 1;\nconst b' });
  });

  it('only closes a fence with an equal-or-longer marker of the same kind', async () => {
    const { parseMarkdown } = await md();
    const [b] = parseMarkdown('````md\n```js\ninner\n```\n````');
    expect(b).toMatchObject({ type: 'code', text: '```js\ninner\n```', closed: true });
    const [t] = parseMarkdown('~~~\n```\nstill code\n~~~');
    expect(t).toMatchObject({ type: 'code', text: '```\nstill code', closed: true });
  });

  it('never produces HTML or links: markup and URLs stay plain text', async () => {
    const { parseMarkdown } = await md();
    const hostile = '<img src=x onerror=alert(1)> [click](javascript:alert(1)) <script>x</script> https://evil.example';
    const [p] = parseMarkdown(hostile);
    expect(p?.type).toBe('p');
    expect(p?.inline?.every((t) => t.t === 'text')).toBe(true);
    expect((p?.inline ?? []).map((t) => t.v).join('')).toBe(hostile);
  });

  it('collects cited pattern ids once, in order, ignoring code', async () => {
    const { citedIds } = await md();
    expect(citedIds('See [#3] and [#1], again [#3].\n\n- item [#7]\n\n```\n[#99]\n```')).toEqual([3, 1, 7]);
    expect(citedIds('[#abc] [# 4] [#12345678901]')).toEqual([]);
  });

  it('is total: odd input never throws', async () => {
    const { parseMarkdown } = await md();
    for (const s of ['', '\n\n', '```', '**', '`', '[#', '- ', '#', '1.', '\r\n```\r\nx\r\n```\r\n', 'a'.repeat(50_000)]) {
      expect(() => parseMarkdown(s)).not.toThrow();
    }
    expect(parseMarkdown(undefined as unknown as string)).toEqual([]);
  });
});

describe('chat routes in the host allow-list', () => {
  it('admit the chat routes', () => {
    for (const [m, p] of [
      ['GET', '/chat/conversations'],
      ['POST', '/chat/conversations'],
      ['DELETE', '/chat/conversations'],
      ['GET', '/chat/conversations/7'],
      ['PATCH', '/chat/conversations/7'],
      ['DELETE', '/chat/conversations/7'],
      ['POST', '/chat/conversations/7/messages'],
      ['GET', '/chat/turns/3'],
      ['POST', '/chat/turns/3/cancel'],
    ] as const) {
      expect(isAllowed(m, p), `${m} ${p}`).toBe(true);
    }
  });

  it('reject malformed or unknown chat routes', () => {
    for (const [m, p] of [
      ['PUT', '/chat/conversations'],
      ['GET', '/chat/conversations/abc'],
      ['GET', '/chat/conversations/7/messages'],
      ['POST', '/chat/turns'],
      ['POST', '/chat/conversations/../tasks'],
      ['GET', '/chat/conversations/7?x=1'],
      ['POST', '/chat/conversations/7/messages/extra'],
    ] as const) {
      expect(isAllowed(m, p), `${m} ${p}`).toBe(false);
    }
  });

  it('asks the user (host-side) before deleting conversations', () => {
    expect(confirmationFor('DELETE', '/chat/conversations/7', undefined)).toMatch(/Delete this conversation/);
    expect(confirmationFor('DELETE', '/chat/conversations', undefined)).toMatch(/ALL chat/);
    expect(confirmationFor('POST', '/chat/conversations/7/messages', { message: 'hi' })).toBeUndefined();
    expect(confirmationFor('GET', '/chat/conversations', undefined)).toBeUndefined();
  });

  it('still requires confirmation for the only path from chat to file changes (starting a task)', () => {
    expect(confirmationFor('POST', '/tasks', { kind: 'generate', request: 'add login' })).toMatch(/Start a "generate" task/);
  });
});

describe('copy and open-in-editor RPC', () => {
  const deps = (over: Partial<RpcDeps> = {}): RpcDeps & { copied: string[]; opened: [string, string | undefined][] } => {
    const copied: string[] = [];
    const opened: [string, string | undefined][] = [];
    return {
      getClient: () => undefined,
      confirm: async () => true,
      saveJson: async () => true,
      runCommand: async () => undefined,
      copyText: async (t) => void copied.push(t),
      openText: async (t, l) => void opened.push([t, l]),
      copied,
      opened,
      ...over,
    };
  };

  it('copies and opens text, validating the language id', async () => {
    const d = deps();
    expect(await handleRpc({ type: 'copy', id: 1, text: 'print(1)' }, d)).toEqual({ type: 'copyResult', id: 1, ok: true });
    expect(await handleRpc({ type: 'openText', id: 2, text: 'x', language: 'Python' }, d)).toEqual({ type: 'openTextResult', id: 2, ok: true });
    expect(await handleRpc({ type: 'openText', id: 3, text: 'x', language: '../../etc/passwd' }, d)).toEqual({ type: 'openTextResult', id: 3, ok: true });
    expect(d.copied).toEqual(['print(1)']);
    expect(d.opened).toEqual([['x', 'python'], ['x', undefined]]); // a bad language id is dropped, not passed on
  });

  it('refuses oversized or non-string text and works without optional deps', async () => {
    const d = deps();
    const big = 'x'.repeat(200_001);
    expect((await handleRpc({ type: 'copy', id: 1, text: big }, d))?.type).toBe('copyResult');
    expect(await handleRpc({ type: 'copy', id: 1, text: big }, d)).toMatchObject({ ok: false });
    expect(await handleRpc({ type: 'openText', id: 2, text: 5 as unknown as string }, d)).toMatchObject({ ok: false });
    expect(await handleRpc({ type: 'copy', id: 3, text: 'x' }, deps({ copyText: undefined }))).toMatchObject({ ok: false });
    expect(d.copied).toEqual([]);
    expect(d.opened).toEqual([]);
  });
});

describe('Ask about selection', () => {
  it('refuses files that look like credentials', () => {
    for (const p of ['.env', 'config/.env.production', 'certs/server.pem', 'keys/id_rsa', '.ssh/config', 'home/.aws/credentials', 'a/b/secrets.yaml', '.npmrc', 'x.p12', 'creds.keystore']) {
      expect(isSensitivePath(p), p).toBe(true);
    }
    for (const p of ['src/app.ts', 'README.md', 'src/environment.ts', 'docs/keys-guide.md', 'src/secretsanta.py', 'env/config.py', '']) {
      expect(isSensitivePath(p), p).toBe(false);
    }
  });

  it('builds a review-before-send prompt with a language-tagged fence and a line range', () => {
    const p = buildAskPrompt({ relativePath: 'src/auth.py', languageId: 'python', startLine: 10, endLine: 14, text: 'def f():\n    return 1\n\n' });
    expect(p).toContain('src/auth.py (lines 10–14)');
    expect(p).toMatch(/```python\ndef f\(\):\n {4}return 1\n```$/);
    expect(buildAskPrompt({ relativePath: 'a.ts', languageId: 'typescript', startLine: 3, endLine: 3, text: 'x' })).toContain('(line 3)');
  });

  it('uses a longer fence when the code itself contains backtick fences', () => {
    const p = buildAskPrompt({ relativePath: 'README.md', languageId: 'markdown', startLine: 1, endLine: 5, text: '```js\nx\n```' });
    expect(p).toMatch(/````markdown\n```js\nx\n```\n````$/);
  });

  it('caps very long selections and says so; sanitises the language id', () => {
    const p = buildAskPrompt({ relativePath: 'big.txt', languageId: 'plain text!!', startLine: 1, endLine: 999, text: 'a'.repeat(MAX_SELECTION_CHARS + 500) });
    expect(p).toContain(`Only the first ${MAX_SELECTION_CHARS} characters`);
    expect(p).toMatch(/```\na{50}/); // language dropped, fence still valid
    expect(p.length).toBeLessThan(MAX_SELECTION_CHARS + 500);
  });
});
