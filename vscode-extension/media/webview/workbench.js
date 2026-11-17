import { api, onEvent, ready } from './lib/api.js';
import { h, clear, badge } from './lib/dom.js';
import { dashboardView } from './views/dashboard.js';
import { learningView } from './views/learning.js';
import { libraryView } from './views/library.js';
import { generateView } from './views/generate.js';
import { settingsView } from './views/settings.js';
import { agentsView } from './views/agents.js';
import { testingView } from './views/testing.js';

const SECTIONS = [
  { id: 'dashboard', label: 'Dashboard', view: dashboardView },
  { id: 'learning', label: 'AI Learning Centre', view: learningView },
  { id: 'library', label: 'Pattern Library', view: libraryView },
  { id: 'generate', label: 'Code Generation', view: generateView },
  { id: 'agents', label: 'Agent Control Centre', view: agentsView },
  { id: 'testing', label: 'Testing Centre', view: testingView },
  { id: 'settings', label: 'Settings', view: settingsView },
];

const state = { section: 'dashboard', notice: null, context: { refresh: () => render(), notify: (text, kind = 'ok') => { state.notice = { text, kind }; } } };
const root = document.getElementById('app');
let cleanup = null;
let renderSeq = 0;

function nav() {
  return h('nav', { class: 'nav', 'aria-label': 'PatternVault sections' },
    h('div', { class: 'brand' }, 'PatternVault'),
    ...SECTIONS.map((s) => h('button', {
      class: `nav-item ${s.id === state.section ? 'active' : ''}`, dataset: { section: s.id },
      'aria-current': s.id === state.section ? 'page' : undefined,
      onClick: () => { state.section = s.id; render(); },
    }, s.label)));
}

async function render() {
  const mine = ++renderSeq;
  if (cleanup) { try { cleanup(); } catch { /* ignore */ } cleanup = null; }
  const section = SECTIONS.find((s) => s.id === state.section) || SECTIONS[0];
  const notice = state.notice;
  state.notice = null;
  const content = h('main', { class: 'content', id: 'content' }, h('div', { class: 'muted' }, 'Loading…'));
  clear(root);
  root.append(nav(), content);
  try {
    const out = await section.view(content, state.context);
    if (notice) content.prepend(h('div', { id: 'notice', class: `notice ${notice.kind}`, role: 'status' }, notice.text));
    if (mine !== renderSeq) { if (typeof out === 'function') out(); return; }
    cleanup = typeof out === 'function' ? out : null;
  } catch (err) {
    clear(content);
    content.append(h('div', { class: 'error', role: 'alert' }, `Could not load ${section.label}: ${err.message}`));
  }
}

onEvent((e) => {
  if (/^(task|approval|testing|files|mode|config|pattern|session|model)\./.test(e.type)) {
    document.dispatchEvent(new CustomEvent('pv:event', { detail: e }));
  }
});

document.addEventListener('pv:event', () => {
  if (state.section === 'dashboard') state.context.refresh();
});

ready();
render();
export { api, badge };
