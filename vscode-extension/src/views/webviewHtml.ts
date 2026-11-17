/** Builds the Workbench HTML with a strict nonce-based CSP. Pure (no `vscode` import). */
export interface HtmlInput {
  cspSource: string;
  nonce: string;
  scriptUri: string;
  styleUri: string;
}

export function renderWorkbenchHtml(i: HtmlInput): string {
  const csp = [
    "default-src 'none'",
    `style-src ${i.cspSource}`,
    `script-src 'nonce-${i.nonce}' ${i.cspSource}`,
    `font-src ${i.cspSource}`,
    `img-src ${i.cspSource} data:`,
    "base-uri 'none'",
    "form-action 'none'",
  ].join('; ');
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="${i.styleUri}">
<title>PatternVault</title>
</head>
<body>
<div id="app" aria-live="polite"></div>
<script type="module" nonce="${i.nonce}" src="${i.scriptUri}"></script>
</body>
</html>`;
}

export function makeNonce(randomBytes: (n: number) => Uint8Array): string {
  return Buffer.from(randomBytes(18)).toString('base64url');
}
