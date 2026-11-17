import { api, copyText, onEvent, onPrefill, openText } from '../lib/api.js';
import { h, clear, badge, statusBadge, emptyState, relTime } from '../lib/dom.js';
import { renderMarkdown } from '../lib/render.js';
import { taskPanel } from '../lib/tasks.js';
import { contextPanel, resultPanel } from './generate.js';

// What the user was doing survives switching Workbench sections (the view is rebuilt each time).
const memory = { conversationId: null, fresh: false, projectId: null, useKnowledge: true, draft: '' };
const TERMINAL = ['verified', 'completed', 'failed', 'cancelled'];
const HELP = [
  ['/search <words>', 'Search your stored patterns'],
  ['/context <request>', 'Show how a request is understood and what knowledge is missing'],
  ['/generate <request>', 'Propose code as a reviewable diff. Nothing is written'],
  ['/task <request>', 'Have the agents apply it: recovery point, tests, auto-fix, rollback. You are asked to confirm'],
  ['/recreate <pattern id>', 'Replay a stored implementation into the project, with tests. You are asked to confirm'],
  ['/status', 'Mode, local model and knowledge-base summary'],
  ['/new', 'Start a new conversation'],
];
const SUGGESTIONS = ['What have you learned so far?', '/search authentication', '/help'];
const trunc = (s, n) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

const errorNode = (e) => h('div', { class: 'error', role: 'alert' }, e && e.message ? e.message : String(e));

export async function chatView(el, ctx, { embedded = false } = {}) {
  const [projects, convs, models, config] = await Promise.all([api.get('/projects'), api.get('/chat/conversations'), api.get('/models/status'), api.get('/config')]);
  let conversations = convs;
  if (memory.conversationId && !conversations.some((c) => c.id === memory.conversationId)) memory.conversationId = null;
  if (memory.conversationId === null && !memory.fresh && conversations.length) memory.conversationId = conversations[0].id;
  if (memory.projectId && !projects.some((p) => p.id === memory.projectId)) memory.projectId = null;
  if (memory.projectId === null && projects.length === 1) memory.projectId = projects[0].id;

  let messages = [];
  const cards = []; // results of slash commands: shown in the transcript, not stored
  const stoppers = [];
  let live = null;
  let busy = null; // null | 'turn' | 'command'
  let disposed = false;

  const lastId = () => (messages.length ? messages[messages.length - 1].id : 0);
  const projectId = () => (memory.projectId ? Number(memory.projectId) : null);

  // ------------------------------------------------------------------ transcript
  const log = h('div', { class: 'chat-log', id: 'chat-log', role: 'log', 'aria-label': 'Conversation', tabindex: 0 });
  const nearBottom = () => log.scrollHeight - log.scrollTop - log.clientHeight < 90;
  const scrollDown = (force) => { if (force) log.scrollTop = log.scrollHeight; };

  const mdHandlers = (sources) => ({ sources, onCite: (id) => showPattern(id), onCopy: copyText, onOpen: openText });

  function patternCard(p) {
    const files = p.content?.files || [];
    return h('div', { class: 'card inner pattern-card', dataset: { patternId: p.id } },
      h('div', { class: 'row wrap' }, h('strong', {}, `#${p.id} ${p.title}`), statusBadge(p.status), badge(`${Math.round(p.confidence * 100)}% confidence`, 'muted'),
        p.language ? badge(p.language, 'muted') : null, p.framework ? badge(`${p.framework} ${p.framework_version || ''}`.trim(), 'muted') : null),
      p.summary ? h('div', { class: 'small' }, p.summary) : null,
      ...files.slice(0, 6).map((f) => h('details', { class: 'file' }, h('summary', {}, h('code', {}, f.path), ' ', badge(f.role || 'source', 'muted')), h('pre', { class: 'code' }, h('code', {}, f.content || '')))));
  }

  async function showPattern(id) {
    try { addCard(patternCard(await api.get(`/patterns/${id}`))); } catch (e) { addCard(errorNode(e)); }
  }

  function messageNode(m) {
    const meta = m.meta || {};
    const who = m.role === 'user' ? 'You' : `PatternVault${meta.model && meta.model_used !== false ? ` · ${meta.model}` : ''}`;
    const body = m.role === 'user'
      ? h('div', { class: 'msg-user-text' }, m.content)
      : m.content ? renderMarkdown(m.content, mdHandlers(m.sources || [])) : null;
    const sources = (m.sources || []).length
      ? h('div', { class: 'msg-sources small' }, h('span', { class: 'muted', title: 'Stored patterns that matched your question and were given to the model as reference. It may not have used them.' }, 'Matched patterns:'),
        ...m.sources.map((s) => h('button', { class: 'cite', type: 'button', title: `${s.title} — ${s.status}, confidence ${Math.round(s.confidence * 100)}%`, onClick: () => showPattern(s.id) }, `#${s.id} ${trunc(s.title, 26)}`)))
      : null;
    const info = [];
    if (m.role === 'assistant') {
      if (meta.model_used === false) info.push('answered from your stored patterns — no language model was used');
      else {
        if (meta.completion_tokens) info.push(`${meta.completion_tokens} tokens`);
        if (meta.duration_ms) info.push(`${(meta.duration_ms / 1000).toFixed(1)} s`);
      }
      if (meta.cancelled) info.push('stopped');
    }
    const notes = [...(meta.notes || [])];
    if (m.role === 'user' && meta.redactions) notes.push(`${meta.redactions} secret-like value(s) in this message were redacted before use.`);
    if (m.role === 'assistant' && meta.redactions_in_question) notes.push(`${meta.redactions_in_question} secret-like value(s) in your question were redacted before it was used.`);
    return h('article', { class: `msg ${m.role}${meta.error ? ' failed' : ''}`, dataset: { role: m.role, messageId: m.id } },
      h('div', { class: 'msg-who muted small' }, who, ' ', h('span', {}, relTime(m.created_at))),
      body, sources,
      ...notes.map((n) => h('div', { class: 'note small' }, n)),
      meta.error ? h('div', { class: 'error small', role: 'alert' }, meta.error) : null,
      info.length ? h('div', { class: 'muted small msg-meta' }, info.join(' · ')) : null);
  }

  function drawLog() {
    const stick = nearBottom();
    clear(log);
    const at = (afterId) => cards.filter((c) => c.afterId === afterId).map((c) => c.node);
    log.append(...at(0));
    for (const m of messages) log.append(messageNode(m), ...at(m.id));
    if (live) log.append(live.node);
    if (!messages.length && !cards.length && !live) log.append(welcome());
    scrollDown(stick);
  }

  function addCard(node, { user = false } = {}) {
    const wrap = h('article', { class: `msg ${user ? 'user' : 'system'}` }, node);
    const card = { afterId: lastId(), node: wrap };
    cards.push(card);
    drawLog();
    scrollDown(true);
    return {
      set(next) { const w = h('article', { class: 'msg system' }, next); card.node.replaceWith(w); card.node = w; },
    };
  }

  function welcome() {
    return h('div', { class: 'welcome' },
      emptyState('Ask PatternVault', 'Ask about the patterns it has learned, or try a command. Type / to see them all.'),
      h('div', { class: 'suggestions' }, SUGGESTIONS.map((s) => h('button', { class: 'chip-btn', type: 'button', onClick: () => { input.value = s; memory.draft = s; input.focus(); } }, s))));
  }

  // ------------------------------------------------------------------ live (streaming) answer
  function renderLive() {
    if (!live) return;
    const stick = nearBottom();
    clear(live.holder);
    live.holder.append(live.text ? renderMarkdown(live.text, mdHandlers([])) : h('div', { class: 'typing muted' }, 'Thinking…'));
    scrollDown(stick);
  }
  function scheduleRender() {
    if (!live || live.raf) return;
    live.raf = requestAnimationFrame(() => { if (live) { live.raf = 0; renderLive(); } });
  }

  function startLive(turnId) {
    stopLive();
    const holder = h('div', { class: 'md-holder' });
    const node = h('article', { class: 'msg assistant live', dataset: { role: 'assistant', turn: turnId }, 'aria-live': 'off', 'aria-busy': 'true' },
      h('div', { class: 'msg-who muted small' }, 'PatternVault'), holder);
    live = { id: turnId, text: '', raf: 0, holder, node, timer: null, polling: false };
    renderLive();
    drawLog();
    scrollDown(true);
    live.timer = setInterval(poll, 1000); // events are the fast path; polling guarantees progress if any are missed
    setBusy('turn');
    poll();
  }
  function stopLive() {
    if (!live) return;
    clearInterval(live.timer);
    cancelAnimationFrame(live.raf);
    live = null;
  }
  async function finishLive() {
    if (!live) return;
    stopLive();
    try { await loadConversation(); } catch (e) { addCard(errorNode(e)); }
    setBusy(null);
  }

  async function poll() {
    const l = live;
    if (!l || l.polling) return;
    l.polling = true;
    try {
      const t = await api.get(`/chat/turns/${l.id}`);
      if (live !== l) return;
      if (t.text.length > l.text.length) { l.text = t.text; scheduleRender(); }
      if (t.status !== 'running') await finishLive();
    } catch (e) {
      if (live === l && e.status === 404) await finishLive(); // the backend restarted: show what was stored
    } finally { l.polling = false; }
  }

  // Deltas carry their offset so events and polling can interleave without duplicating or losing text.
  function applyDelta(delta, offset) {
    if (!live) return;
    if (offset > live.text.length) { poll(); return; }
    const skip = live.text.length - offset;
    if (skip < delta.length) { live.text += delta.slice(skip); scheduleRender(); }
  }
  const offEvents = onEvent((e) => {
    const d = e.data || {};
    if (!live || d.turn_id !== live.id) return;
    if (e.type === 'chat.turn.delta') applyDelta(String(d.delta || ''), Number(d.offset || 0));
    else if (e.type === 'chat.turn.done') finishLive();
  });

  // ------------------------------------------------------------------ composer and header
  const input = h('textarea', { id: 'chat-input', rows: 3, placeholder: 'Ask about your patterns, or type / for commands', 'aria-label': 'Message', maxlength: 8000 });
  input.value = memory.draft;
  input.addEventListener('input', () => { memory.draft = input.value; });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  const offPrefill = onPrefill((text) => { input.value = text; memory.draft = text; input.focus(); input.setSelectionRange(text.length, text.length); });

  const sendBtn = h('button', { class: 'btn', id: 'chat-send', type: 'button', onClick: () => (busy === 'turn' ? stop() : send()) }, 'Send');
  function setBusy(kind) {
    busy = kind;
    sendBtn.textContent = kind === 'turn' ? 'Stop' : kind === 'command' ? 'Working…' : 'Send';
    sendBtn.disabled = kind === 'command';
    sendBtn.classList.toggle('secondary', kind === 'turn');
    deleteBtn.disabled = !memory.conversationId || kind !== null;
    convSel.disabled = kind !== null;
    newBtn.disabled = kind !== null;
  }
  async function stop() {
    if (!live) return;
    try { await api.post(`/chat/turns/${live.id}/cancel`); } catch (e) { addCard(errorNode(e)); }
  }

  const knowledge = h('input', { type: 'checkbox', id: 'chat-knowledge', checked: memory.useKnowledge, onChange: (e) => { memory.useKnowledge = e.target.checked; } });
  const convSel = h('select', { id: 'chat-conv', 'aria-label': 'Conversation', onChange: async (e) => {
    memory.conversationId = e.target.value ? Number(e.target.value) : null;
    memory.fresh = !memory.conversationId;
    await loadConversation();
  } });
  const projectSel = h('select', { id: 'chat-project', 'aria-label': 'Project', title: 'The project the conversation is about', onChange: async (e) => {
    memory.projectId = e.target.value ? Number(e.target.value) : null;
    if (memory.conversationId) { try { await api.patch(`/chat/conversations/${memory.conversationId}`, { project_id: memory.projectId }); } catch (err) { addCard(errorNode(err)); } }
  } }, h('option', { value: '' }, '(no project)'), ...projects.map((p) => h('option', { value: p.id }, p.name)));
  const newBtn = h('button', { class: 'btn small secondary', id: 'chat-new', type: 'button', onClick: () => newChat() }, 'New chat');
  const deleteBtn = h('button', { class: 'btn small secondary', id: 'chat-delete', type: 'button', title: 'Delete this conversation', onClick: async () => {
    if (!memory.conversationId) return;
    try { await api.del(`/chat/conversations/${memory.conversationId}`); } catch (e) { if (e.status !== 499) addCard(errorNode(e)); return; }
    newChat(); await refreshConversations();
  } }, 'Delete');

  function fillConversationSelect() {
    clear(convSel);
    convSel.append(h('option', { value: '' }, '(new chat)'), ...conversations.map((c) => h('option', { value: c.id }, trunc(c.title, 48))));
    convSel.value = memory.conversationId ? String(memory.conversationId) : '';
    projectSel.value = memory.projectId ? String(memory.projectId) : '';
  }
  async function refreshConversations() {
    conversations = await api.get('/chat/conversations');
    fillConversationSelect();
  }
  function newChat() {
    stopLive();
    memory.conversationId = null;
    memory.fresh = true;
    shownConversation = null;
    messages = [];
    cards.length = 0;
    fillConversationSelect();
    drawLog();
    setBusy(null);
    input.focus();
  }

  let shownConversation = null;
  async function loadConversation() {
    if (shownConversation !== memory.conversationId) { cards.length = 0; shownConversation = memory.conversationId; }
    if (!memory.conversationId) {
      messages = [];
      fillConversationSelect();
      drawLog();
      if (!live) setBusy(null);
      return;
    }
    const c = await api.get(`/chat/conversations/${memory.conversationId}`);
    messages = c.messages;
    if (c.project_id) memory.projectId = c.project_id;
    await refreshConversations();
    drawLog();
    if (c.running_turn && !live) startLive(c.running_turn);
    else if (!live) setBusy(null);
  }

  async function ensureConversation() {
    if (memory.conversationId) return;
    const c = await api.post('/chat/conversations', { project_id: projectId() });
    memory.conversationId = c.id;
    memory.fresh = false;
    shownConversation = c.id; // keep the cards of the command that just ran
    await refreshConversations();
  }

  // ------------------------------------------------------------------ sending and slash commands
  async function send() {
    const text = input.value.trim();
    if (!text || busy) return;
    if (text.startsWith('/')) { input.value = ''; memory.draft = ''; await runSlash(text); return; }
    setBusy('command');
    try {
      await ensureConversation();
      const r = await api.post(`/chat/conversations/${memory.conversationId}/messages`, { message: text, use_knowledge: knowledge.checked });
      input.value = '';
      memory.draft = '';
      const c = await api.get(`/chat/conversations/${memory.conversationId}`);
      messages = c.messages;
      await refreshConversations();
      startLive(r.turn_id);
    } catch (e) {
      setBusy(null);
      if (e.status === 404) { memory.conversationId = null; await refreshConversations(); }
      addCard(errorNode(new Error(`Could not send: ${e.message}`)));
    }
  }

  const needProject = () => {
    if (projectId()) return true;
    addCard(h('div', { class: 'note' }, 'Choose a project first (the selector at the top). This command works on a project’s files.'));
    return false;
  };

  function trackTask(id) {
    const card = addCard(h('div', { class: 'muted', role: 'status' }, `Starting task ${id}…`));
    let timer = null;
    let stopped = false;
    stoppers.push(() => { stopped = true; clearTimeout(timer); });
    const draw = async () => {
      clearTimeout(timer);
      if (stopped || disposed) return;
      let t;
      try { t = await api.get(`/tasks/${id}`); } catch (e) { card.set(errorNode(e)); return; }
      card.set(await taskPanel(t, draw));
      if (!TERMINAL.includes(t.status)) timer = setTimeout(draw, 1500);
    };
    draw();
  }

  async function startTask(kind, request, params) {
    try {
      const r = await api.post('/tasks', { kind, request, project_id: projectId(), params });
      trackTask(r.id);
    } catch (e) {
      addCard(e.status === 499 ? h('div', { class: 'muted' }, 'Cancelled — nothing was started.') : errorNode(e));
    }
  }

  const COMMANDS = {
    help: async () => { addCard(h('div', {}, h('strong', {}, 'Commands'), h('dl', { class: 'kv' }, HELP.flatMap(([c, d]) => [h('dt', {}, h('code', {}, c)), h('dd', {}, d)])), h('div', { class: 'muted small' }, 'Anything else is a question for the assistant. It can read your stored patterns but cannot change files; /task is the only way to make changes, and it asks first.'))); },
    new: async () => newChat(),
    clear: async () => newChat(),
    status: async () => {
      const d = await api.get('/dashboard');
      const lm = d.local_model;
      addCard(h('dl', { class: 'kv', id: 'chat-status-card' },
        h('dt', {}, 'Mode'), h('dd', {}, d.mode), h('dt', {}, 'Permissions'), h('dd', {}, d.permission_level.replace(/_/g, ' ')),
        h('dt', {}, 'Local model'), h('dd', {}, lm.ready ? `ready: ${lm.selected}` : lm.reachable ? `not ready: ${lm.reason}` : 'Ollama is not running'),
        h('dt', {}, 'Patterns'), h('dd', {}, `${d.counts.patterns} stored, ${d.counts.verified_patterns} verified`), h('dt', {}, 'Sessions'), h('dd', {}, String(d.counts.sessions))));
    },
    search: async (arg) => {
      if (!arg) { addCard(h('div', { class: 'note' }, 'Usage: /search <words>')); return; }
      const r = await api.post('/search', { query: arg, project_id: projectId(), top_k: 8 });
      addCard(h('div', { class: 'stack', id: 'chat-search-results' }, h('strong', {}, `Results for “${trunc(arg, 60)}”`),
        r.hits.length ? h('ul', { class: 'list' }, r.hits.map((x) => h('li', { dataset: { patternId: x.pattern_id } },
          h('div', { class: 'row wrap' }, h('strong', {}, x.pattern.title), statusBadge(x.pattern.status), h('span', { class: 'muted small' }, `score ${x.score.toFixed(2)}`)),
          x.warnings.map((w) => h('div', { class: 'warn small' }, w)),
          h('div', { class: 'row' }, h('button', { class: 'btn small secondary', type: 'button', onClick: () => showPattern(x.pattern_id) }, 'Show'),
            h('button', { class: 'btn small secondary', type: 'button', onClick: () => { input.value = `Explain pattern #${x.pattern_id} “${x.pattern.title}” and how I would reuse it here.`; memory.draft = input.value; input.focus(); } }, 'Ask about this'))))) : h('div', { class: 'muted' }, 'No matching patterns.')));
    },
    context: async (arg) => {
      if (!arg) { addCard(h('div', { class: 'note' }, 'Usage: /context <request>')); return; }
      addCard(contextPanel(await api.post('/context', { request: arg, project_id: projectId() })));
    },
    generate: async (arg) => {
      if (!arg) { addCard(h('div', { class: 'note' }, 'Usage: /generate <what to build>')); return; }
      if (!needProject()) return;
      const card = addCard(h('div', { class: 'muted', role: 'status' }, 'Generating with the local model… this can take a while on CPU.'));
      try {
        const r = await api.post('/generate', { request: arg, project_id: projectId(), mode: 'auto', backend: 'auto' });
        const panels = [r.result, ...r.alternatives];
        card.set(h('div', { class: 'stack', id: 'chat-generate-result' },
          r.resolution.warnings.length ? h('div', { class: 'note' }, r.resolution.warnings.join(' · ')) : null,
          ...panels.map((p) => resultPanel(p, panels.length > 1 ? `${p.backend} · ${p.model}` : undefined)),
          h('div', { class: 'muted small' }, 'Nothing was written to your project. Use /task to apply a change with tests and a recovery point.')));
      } catch (e) { card.set(errorNode(e)); }
    },
    task: async (arg) => {
      if (!arg) { addCard(h('div', { class: 'note' }, 'Usage: /task <what to build>')); return; }
      if (!needProject()) return;
      await startTask('generate', arg, {});
    },
    recreate: async (arg) => {
      if (!/^#?\d+$/.test(arg)) { addCard(h('div', { class: 'note' }, 'Usage: /recreate <pattern id>   (see /search for ids)')); return; }
      if (!needProject()) return;
      const id = Number(arg.replace('#', ''));
      await startTask('replay', `Recreate pattern ${id}`, { pattern_id: id });
    },
  };

  async function runSlash(raw) {
    const m = /^\/([A-Za-z]+)\s*([\s\S]*)$/.exec(raw);
    const name = m ? m[1].toLowerCase() : '';
    addCard(h('div', { class: 'msg-user-text' }, raw), { user: true });
    const run = COMMANDS[name];
    if (!run) { addCard(h('div', { class: 'note' }, `Unknown command “/${name}”. Type /help to see what is available.`)); return; }
    setBusy('command');
    try { await run((m[2] || '').trim()); } catch (e) { addCard(errorNode(e)); } finally { if (!live) setBusy(null); }
  }

  // ------------------------------------------------------------------ assemble
  const modelStatus = models.reachable && models.ready
    ? h('div', { class: 'ok small', id: 'chat-model-status' }, `Local model ready: ${models.selected} · ${config.mode} mode`)
    : h('div', { class: 'warn small', id: 'chat-model-status' }, `No local model is ready (${models.reason || 'Ollama is not reachable'}). I can still search your stored patterns and answer from them.`);
  fillConversationSelect();
  clear(el);
  el.append(h('section', { class: `chat${embedded ? ' embedded' : ''}`, id: 'chat', 'aria-label': 'PatternVault chat' },
    h('div', { class: 'chat-head' }, convSel, newBtn, deleteBtn, projectSel),
    modelStatus, log,
    h('div', { class: 'chat-composer' }, input,
      h('div', { class: 'row wrap' }, h('label', { class: 'row small', title: 'Retrieve relevant patterns from your knowledge base for each question' }, knowledge, 'Use my patterns'),
        h('span', { class: 'muted small grow' }, 'Enter to send · Shift+Enter for a new line · / for commands'), sendBtn))));
  setBusy(null);
  await loadConversation();
  if (!embedded) input.focus();

  return () => {
    disposed = true;
    offEvents();
    offPrefill();
    stopLive();
    for (const s of stoppers) s();
  };
}
