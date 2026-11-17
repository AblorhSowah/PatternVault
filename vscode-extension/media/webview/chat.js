// Entry point of the chat docked in the side bar (the same view is also a Workbench section).
import { ready } from './lib/api.js';
import { h, clear } from './lib/dom.js';
import { chatView } from './views/chat.js';

document.body.classList.add('chat-only');
const root = document.getElementById('app');
let cleanup = null;
let seq = 0;

async function mount() {
  const mine = ++seq;
  if (cleanup) { try { cleanup(); } catch { /* ignore */ } cleanup = null; }
  const holder = h('div', { id: 'content' });
  clear(root);
  root.append(holder);
  try {
    const out = await chatView(holder, { refresh: mount, notify: () => {} });
    if (mine !== seq) { if (typeof out === 'function') out(); return; }
    cleanup = typeof out === 'function' ? out : null;
  } catch (err) {
    clear(holder);
    holder.append(h('div', { class: 'chat', style: 'height:auto' },
      h('div', { class: 'warn', role: 'alert' }, `The chat could not reach the PatternVault backend: ${err.message}`),
      h('div', { class: 'muted small' }, 'If it is not installed yet, run “PatternVault: Set Up Backend”. Retrying automatically…'),
      h('button', { class: 'btn small', type: 'button', onClick: () => mount() }, 'Retry now')));
    setTimeout(() => { if (mine === seq) mount(); }, 4000);
  }
}

ready();
mount();
