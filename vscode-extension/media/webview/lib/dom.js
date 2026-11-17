// Native Element.append/prepend turn `null` into the literal text "null". Conditional children are everywhere in the views
// (`cond ? node : null`), so make both tolerant: null/undefined/false are skipped.
for (const name of ['append', 'prepend']) {
  const native = Element.prototype[name];
  Element.prototype[name] = function patched(...nodes) {
    return native.apply(this, nodes.flat(Infinity).filter((n) => n !== null && n !== undefined && n !== false));
  };
}

// Tiny DOM helper. All text goes through textContent / text nodes - never innerHTML (content is untrusted).
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'value') el.value = v;
    else if (k === 'checked' || k === 'disabled' || k === 'selected') el[k] = Boolean(v);
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function badge(text, kind = '') {
  return h('span', { class: `badge ${kind}` }, text);
}

export function statusBadge(status) {
  const kind = { verified: 'ok', tested: 'ok', approved: 'info', observed: 'muted', generated: 'muted', deprecated: 'bad' }[status] || 'muted';
  return badge(status, kind);
}

export function confidenceBar(value) {
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
  const bar = h('div', { class: 'bar', role: 'meter', 'aria-valuenow': pct, 'aria-valuemin': 0, 'aria-valuemax': 100, title: `confidence ${pct}%` });
  const fill = h('div', { class: `bar-fill ${pct >= 80 ? 'ok' : pct >= 50 ? 'info' : 'low'}` });
  fill.style.width = `${pct}%`;
  bar.appendChild(fill);
  return bar;
}

export function relTime(iso) {
  if (!iso) return '';
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

export function debounce(fn, ms) {
  let t;
  return (...a) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...a), ms);
  };
}

export function emptyState(title, hint) {
  return h('div', { class: 'empty' }, h('div', { class: 'empty-title' }, title), hint ? h('div', { class: 'muted' }, hint) : null);
}

// Renders a unified diff with +/- line colouring. Text only (textContent) - never HTML.
export function diffView(diff) {
  const box = h('pre', { class: 'code diff' });
  if (!diff) { box.append(h('span', { class: 'muted' }, '(no changes)')); return box; }
  for (const line of diff.split('\n')) {
    if (line === '') continue;
    const cls = line.startsWith('+++') || line.startsWith('---') ? 'diff-head' : line.startsWith('@@') ? 'diff-hunk' : line.startsWith('+') ? 'diff-add' : line.startsWith('-') ? 'diff-del' : '';
    box.append(h('div', { class: `diff-line ${cls}` }, line));
  }
  return box;
}

export function issueList(issues) {
  if (!issues || !issues.length) return h('div', { class: 'ok small' }, 'No problems detected by the automatic checks.');
  return h('ul', { class: 'list' }, issues.map((i) => h('li', {}, badge(i.level, i.level === 'error' ? 'bad' : i.level === 'warning' ? 'warn' : 'muted'), ' ', i.path ? h('code', {}, i.path) : null, ' ', i.message)));
}
