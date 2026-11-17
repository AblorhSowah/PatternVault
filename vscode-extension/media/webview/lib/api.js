// All backend access goes through the extension host (the API token never reaches the webview).
const vscode = acquireVsCodeApi();
const pending = new Map();
let seq = 0;
const eventListeners = new Set();
const prefillListeners = new Set();
let pendingPrefill = null; // a prefill that arrived before the chat view had finished loading

window.addEventListener('message', (ev) => {
  const m = ev.data;
  if (!m || typeof m !== 'object') return;
  if (m.type === 'apiResult' || m.type === 'confirmResult' || m.type === 'saveResult' || m.type === 'commandResult' || m.type === 'copyResult' || m.type === 'openTextResult') {
    const p = pending.get(m.id);
    if (!p) return;
    pending.delete(m.id);
    if (m.type === 'apiResult' && !m.ok) p.reject(Object.assign(new Error(m.error || `HTTP ${m.status}`), { status: m.status }));
    else p.resolve(m.type === 'apiResult' ? m.data : m);
  } else if (m.type === 'event') {
    for (const l of eventListeners) l(m.event);
  } else if (m.type === 'prefill' && typeof m.text === 'string') {
    if (prefillListeners.size) for (const l of prefillListeners) l(m.text);
    else pendingPrefill = m.text;
  }
});

function call(message) {
  const id = ++seq;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    vscode.postMessage({ ...message, id });
  });
}

export const api = {
  get: (path, query) => call({ type: 'api', method: 'GET', path, query }),
  post: (path, body, query) => call({ type: 'api', method: 'POST', path, body, query }),
  put: (path, body) => call({ type: 'api', method: 'PUT', path, body }),
  patch: (path, body) => call({ type: 'api', method: 'PATCH', path, body }),
  del: (path, query) => call({ type: 'api', method: 'DELETE', path, query }),
};

export const host = {
  confirm: async (message) => (await call({ type: 'confirm', message })).ok === true,
  saveJson: (suggestedName, data) => call({ type: 'saveJson', suggestedName, data }),
  command: (command) => call({ type: 'command', command }),
};

export async function copyText(text) {
  const r = await call({ type: 'copy', text });
  if (!r.ok) throw new Error('copy failed');
}

export async function openText(text, language) {
  const r = await call({ type: 'openText', text, language });
  if (!r.ok) throw new Error('could not open an editor');
}

/** Text the extension wants placed in the chat input (e.g. "Ask about selection"). Replays one that arrived early. */
export function onPrefill(fn) {
  prefillListeners.add(fn);
  if (pendingPrefill !== null) { const t = pendingPrefill; pendingPrefill = null; setTimeout(() => fn(t), 0); }
  return () => prefillListeners.delete(fn);
}

export function onEvent(fn) {
  eventListeners.add(fn);
  return () => eventListeners.delete(fn);
}

export function ready() {
  vscode.postMessage({ type: 'ready' });
}
