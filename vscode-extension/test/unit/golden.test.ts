/**
 * The Playwright E2E suite (tests/e2e) renders the Workbench from test/fixtures/shell.golden.html. This test pins
 * that file to the real renderWorkbenchHtml() output so the two can never drift. Regenerate with UPDATE_GOLDEN=1.
 */
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import * as path from 'node:path';
import { expect, it } from 'vitest';
import { renderWorkbenchHtml } from '../../src/views/webviewHtml';

const GOLDEN = path.resolve(__dirname, '..', 'fixtures', 'shell.golden.html');

it('shell.golden.html matches renderWorkbenchHtml', () => {
  const html = renderWorkbenchHtml({ cspSource: '{{CSP_SOURCE}}', nonce: '{{NONCE}}', scriptUri: '{{SCRIPT_URI}}', styleUri: '{{STYLE_URI}}' });
  if (process.env.UPDATE_GOLDEN || !existsSync(GOLDEN)) writeFileSync(GOLDEN, html);
  expect(readFileSync(GOLDEN, 'utf8')).toBe(html);
});
