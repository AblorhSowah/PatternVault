import { h } from './dom.js';
import { parseMarkdown } from './markdown.js';

// DOM rendering of the parsed Markdown tree. Text nodes only - never HTML - and no clickable links.

/**
 * @param {string} text
 * @param {{sources?: {id: number, title: string, status: string}[], onCite?: (id: number) => void,
 *   onCopy?: (text: string) => Promise<void> | void, onOpen?: (text: string, lang: string) => Promise<void> | void}} [opts]
 */
export function renderMarkdown(text, opts = {}) {
  const { sources = [], onCite, onCopy, onOpen } = opts;
  const known = new Map(sources.map((s) => [s.id, s]));

  const cite = (id) => {
    const s = known.get(id);
    if (!s) return h('span', { class: 'cite unknown', title: 'Not one of the patterns retrieved for this answer — treat with care' }, `#${id}`);
    return h('button', { class: 'cite', type: 'button', title: `${s.title} (${s.status})`, onClick: () => onCite && onCite(id) }, `#${id}`);
  };
  const inline = (tokens) => tokens.map((t) => (t.t === 'code' ? h('code', { class: 'inline' }, t.v) : t.t === 'strong' ? h('strong', {}, t.v) : t.t === 'cite' ? cite(t.id) : t.v));

  const codeBlock = (b) => {
    const flash = (btn, label) => { const old = btn.textContent; btn.textContent = label; setTimeout(() => { btn.textContent = old; }, 1400); };
    const copy = h('button', { class: 'btn small secondary', type: 'button', onClick: async (ev) => { try { await onCopy(b.text); flash(ev.target, 'Copied'); } catch { flash(ev.target, 'Copy failed'); } } }, 'Copy');
    const open = h('button', { class: 'btn small secondary', type: 'button', title: 'Open in a new, unsaved editor tab', onClick: async () => { try { await onOpen(b.text, b.lang); } catch { /* the host shows its own error */ } } }, 'Open in editor');
    return h('div', { class: 'codeblock' },
      h('div', { class: 'codeblock-head' }, h('span', { class: 'muted small' }, b.lang || 'code'), h('span', { class: 'spacer' }), onCopy ? copy : null, onOpen ? open : null),
      h('pre', { class: 'code' }, h('code', {}, b.text)));
  };

  const root = h('div', { class: 'md' });
  for (const b of parseMarkdown(text)) {
    if (b.type === 'code') root.append(codeBlock(b));
    else if (b.type === 'h') root.append(h(`h${Math.min(6, b.level + 2)}`, { class: 'md-h' }, ...inline(b.inline)));
    else if (b.type === 'ul' || b.type === 'ol') root.append(h(b.type, { class: 'md-list' }, ...b.items.map((it) => h('li', {}, ...inline(it)))));
    else root.append(h('p', {}, ...inline(b.inline)));
  }
  return root;
}
