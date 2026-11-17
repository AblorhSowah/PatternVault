/** Pure helpers for "Ask PatternVault about the selection" (no `vscode` import, unit-tested). */

export const MAX_SELECTION_CHARS = 12_000;

const SENSITIVE_NAMES = [
  /^\.env(\..*)?$/i,
  /\.(pem|key|p12|pfx|jks|keystore|kdbx|ppk)$/i,
  /^id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$/i,
  /^\.(npmrc|pypirc|netrc|pgpass|git-credentials)$/i,
  /^(credentials|secrets?)(\.[a-z0-9]+)?$/i,
  /^\.?(aws|gcloud|azure|kube|ssh|gnupg)$/i,
];

/** True for files that commonly hold credentials. They are never placed into the chat box automatically. */
export function isSensitivePath(relativePath: string): boolean {
  const parts = relativePath.split(/[\\/]+/).filter(Boolean);
  if (parts.length === 0) return false;
  const name = parts[parts.length - 1] as string;
  if (SENSITIVE_NAMES.some((re) => re.test(name))) return true;
  return parts.slice(0, -1).some((dir) => /^\.?(ssh|gnupg|aws|kube)$/i.test(dir));
}

export interface AskInput {
  relativePath: string;
  languageId: string;
  startLine: number; // 1-based
  endLine: number; // 1-based
  text: string;
}

/** Builds the text placed in the chat box. The user reviews and edits it before sending; the backend redacts secrets. */
export function buildAskPrompt(i: AskInput): string {
  let code = i.text.replace(/\r\n/g, '\n').replace(/\s+$/, '');
  let note = '';
  if (code.length > MAX_SELECTION_CHARS) {
    code = code.slice(0, MAX_SELECTION_CHARS);
    note = `\n(Only the first ${MAX_SELECTION_CHARS} characters of the selection were included.)`;
  }
  // A fence longer than any backtick run inside the code cannot be closed by the code itself.
  const longest = Math.max(2, ...[...code.matchAll(/`+/g)].map((m) => m[0].length));
  const fence = '`'.repeat(longest + 1);
  const lang = /^[a-z0-9+#._-]{1,30}$/i.test(i.languageId) ? i.languageId : '';
  const where = i.startLine === i.endLine ? `line ${i.startLine}` : `lines ${i.startLine}–${i.endLine}`;
  return `Explain this code from ${i.relativePath} (${where}), and tell me if my stored patterns have a better way to do it:\n\n${fence}${lang}\n${code}\n${fence}${note}`;
}
