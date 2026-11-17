import { api } from '../lib/api.js';
import { h, clear, badge, statusBadge, diffView, issueList, emptyState } from '../lib/dom.js';
import { taskPanel, listenForTaskEvents } from '../lib/tasks.js';

function field(label, control) {
  return h('label', { class: 'field' }, h('span', { class: 'muted small' }, label), control);
}

function fileCard(f) {
  const kind = { create: 'info', modify: 'warn', identical: 'muted', conflict: 'warn', blocked: 'bad' }[f.action] || 'muted';
  return h('details', { class: 'file', open: f.action !== 'identical' },
    h('summary', {}, h('code', {}, f.path || f.target_path), ' ', badge(f.action, kind), f.needs_approval ? badge('needs approval', 'warn') : null,
      ...(f.issues || []).filter((i) => i.level === 'error').map(() => badge('error', 'bad'))),
    f.reason ? h('div', { class: 'muted small' }, f.reason) : null,
    f.diff ? diffView(f.diff) : h('pre', { class: 'code' }, h('code', {}, f.content || '')),
    f.issues && f.issues.length ? issueList(f.issues) : null);
}

function resultPanel(r, label) {
  return h('div', { class: 'card inner result-panel', dataset: { backend: r.backend } },
    h('div', { class: 'row' }, h('strong', {}, label || r.backend), badge(r.model, 'muted'), badge(`${r.attempts} attempt(s)`, 'muted'), badge(`${r.completion_tokens} tokens`, 'muted'),
      h('span', { class: 'muted small' }, `${(r.duration_ms / 1000).toFixed(1)} s`)),
    r.explanation ? h('p', { class: 'explanation' }, r.explanation) : null,
    r.warnings.length ? h('div', { class: 'note' }, r.warnings.join(' · ')) : null,
    r.dependencies.length ? h('div', {}, h('strong', {}, 'New dependencies: '), r.dependencies.map((d) => h('code', { class: 'chip' }, `${d.name} ${d.version}`.trim()))) : null,
    h('h4', {}, 'Automatic checks'), issueList(r.issues),
    h('h4', {}, `Proposed file changes (${r.files.length})`), ...r.files.map(fileCard));
}

function contextPanel(ctx) {
  return h('div', { class: 'card inner', id: 'context-report' },
    h('div', { class: 'row wrap' }, h('strong', {}, 'Request understood as:'), badge(ctx.intent, 'info'),
      ctx.project?.framework ? badge(`${ctx.project.framework} ${ctx.project.framework_version || ''}`.trim(), 'muted') : null, ctx.project?.language ? badge(ctx.project.language, 'muted') : null),
    ctx.missing.length ? h('ul', { class: 'list' }, ctx.missing.map((m) => h('li', {}, badge(m.severity, m.severity === 'blocker' ? 'bad' : m.severity === 'warning' ? 'warn' : 'muted'), ' ', m.message))) : h('div', { class: 'ok small' }, 'No missing context detected.'),
    ctx.hits.length ? h('div', {}, h('strong', {}, 'Relevant knowledge'), h('ul', { class: 'list' }, ctx.hits.map((x) => h('li', { dataset: { patternId: x.pattern_id } },
      x.pattern.title, ' ', statusBadge(x.pattern.status), ' ', h('span', { class: 'muted small' }, `score ${x.score.toFixed(2)} · ${x.reasons.slice(0, 3).join(', ')}`),
      ...x.warnings.map((w) => h('div', { class: 'warn small' }, w)))))) : h('div', { class: 'muted small' }, 'No matching knowledge.'));
}

export async function generateView(el, ctx) {
  const [projects, models, patterns] = await Promise.all([api.get('/projects'), api.get('/models'), api.get('/patterns', { limit: 100 })]);
  const config = await api.get('/config');
  clear(el);
  const out = h('div', { id: 'gen-output', class: 'stack' });
  const taskOut = h('div', { id: 'task-output', class: 'stack' });
  let watching = null;
  const busy = (on) => { for (const b of el.querySelectorAll('button.act')) b.disabled = on; };
  const show = (...nodes) => { clear(out); out.append(...nodes); };
  const fail = (e) => show(h('div', { class: 'error', role: 'alert' }, e.message));

  const project = h('select', { id: 'g-project' }, [h('option', { value: '' }, '(no project)'), ...projects.map((p) => h('option', { value: p.id }, p.name))]);
  const modelSel = h('select', { id: 'g-model' }, [h('option', { value: '' }, models.selected ? `default (${models.selected})` : 'no model selected'), ...models.installed.map((m) => h('option', { value: m.name }, `${m.name} · ${m.parameter_size || ''}`))]);
  const backend = h('select', { id: 'g-backend' }, ['auto', 'local', 'cloud', 'compare'].map((b) => h('option', { value: b }, b)));
  const modeSel = h('select', { id: 'g-mode' }, ['auto', 'new', 'adapt'].map((b) => h('option', { value: b }, b === 'auto' ? 'auto-detect' : b)));
  const text = h('textarea', { id: 'g-request', rows: 4, placeholder: 'Describe what to build or which implementation to recreate or adapt…' });
  const picked = new Set();
  const sources = h('div', { class: 'stack', id: 'g-sources' });
  const refreshSources = (list) => {
    clear(sources);
    if (!list.length) { sources.append(h('div', { class: 'muted small' }, 'Automatic: the best matches are chosen for you.')); return; }
    for (const p of list) {
      sources.append(h('label', { class: 'row small' }, h('input', { type: 'checkbox', dataset: { patternId: p.id }, checked: picked.has(p.id), onChange: (e) => (e.target.checked ? picked.add(p.id) : picked.delete(p.id)) }),
        `${p.title} `, statusBadge(p.status)));
    }
  };
  refreshSources([]);
  const body = () => ({ request: text.value, project_id: project.value ? Number(project.value) : null, mode: modeSel.value, backend: backend.value,
    model: modelSel.value || null, pattern_ids: picked.size ? [...picked] : null });

  const analyze = h('button', { class: 'btn secondary act', id: 'btn-analyze', onClick: async () => {
    busy(true); try { show(contextPanel(await api.post('/context', { request: text.value, project_id: project.value ? Number(project.value) : null }))); } catch (e) { fail(e); } finally { busy(false); }
  } }, 'Analyze context');
  const find = h('button', { class: 'btn secondary act', id: 'btn-find', onClick: async () => {
    busy(true); try { const r = await api.post('/search', { query: text.value, project_id: project.value ? Number(project.value) : null, top_k: 10 }); refreshSources(r.hits.map((x) => x.pattern)); } catch (e) { fail(e); } finally { busy(false); }
  } }, 'Choose knowledge…');
  const gen = h('button', { class: 'btn act', id: 'btn-generate', onClick: async () => {
    busy(true); show(h('div', { class: 'muted', role: 'status' }, 'Generating with the local model… this can take a while on CPU.'));
    try {
      const r = await api.post('/generate', body());
      const panels = [r.result, ...r.alternatives];
      const nodes = [contextPanel(r.context)];
      if (r.resolution.warnings.length) nodes.push(h('div', { class: 'note' }, r.resolution.warnings.join(' · ')));
      if (panels.length > 1) nodes.push(h('div', { class: 'compare', id: 'compare-view' }, ...panels.map((p) => resultPanel(p, `${p.backend} · ${p.model}`))));
      else nodes.push(resultPanel(r.result));
      if (r.adaptation) nodes.push(h('div', { class: 'card inner', id: 'adaptation' }, h('strong', {}, 'What changed versus the reference pattern(s)'),
        h('div', { class: 'muted small' }, `adapted ${r.adaptation.adapted.length} · new ${r.adaptation.new_files.length} · unchanged copies ${r.adaptation.unchanged_copies.length}`)));
      show(...nodes);
    } catch (e) { fail(e); } finally { busy(false); }
  } }, 'Generate');
  const replayPattern = h('select', { id: 'g-replay-pattern', 'aria-label': 'Pattern to recreate' }, [h('option', { value: '' }, 'Recreate a stored pattern…'), ...patterns.items.map((p) => h('option', { value: p.id }, `${p.title} (${p.status})`))]);
  const replay = h('button', { class: 'btn secondary act', id: 'btn-replay', onClick: async () => {
    if (!replayPattern.value || !project.value) { fail(new Error('Choose a project and a pattern to recreate.')); return; }
    busy(true);
    try {
      const plan = await api.post('/replay/plan', { pattern_id: Number(replayPattern.value), project_id: Number(project.value) });
      show(h('div', { class: 'card inner', id: 'replay-plan' },
        h('div', { class: 'row wrap' }, h('strong', {}, plan.pattern.title), statusBadge(plan.pattern.status), plan.environment.compatible ? badge('compatible', 'ok') : badge('incompatible', 'bad')),
        plan.warnings.length ? h('ul', { class: 'list' }, plan.warnings.map((w) => h('li', { class: 'warn' }, w))) : null,
        plan.dependencies.missing.length ? h('div', {}, h('strong', {}, 'Dependencies to add: '), plan.dependencies.missing.map((d) => h('code', { class: 'chip' }, `${d.name} ${d.version || ''}`.trim()))) : null,
        Object.keys(plan.suggested_path_map).length ? h('div', { class: 'note' }, `Suggested path mapping: ${JSON.stringify(plan.suggested_path_map)}`) : null,
        ...plan.files.map(fileCard)));
    } catch (e) { fail(e); } finally { busy(false); }
  } }, 'Plan recreation');

  let timer = null;
  const TERMINAL = ['verified', 'completed', 'failed', 'cancelled'];
  const showTask = async (id) => {
    watching = id;
    clearTimeout(timer);
    const draw = async () => { const t = await api.get(`/tasks/${id}`); clear(taskOut); taskOut.append(await taskPanel(t, draw)); return t; };
    const loop = async () => {
      let t;
      try { t = await draw(); } catch { return; }
      // Poll as a fallback: pushed events can be missed (reconnects, hidden tabs), and a stuck panel would look like a hung task.
      if (watching === id && !TERMINAL.includes(t.status)) timer = setTimeout(loop, 1500);
    };
    await loop();
  };
  const runTask = (kind, params) => async (ev) => {
    if (!project.value) { fail(new Error('Choose a project first.')); return; }
    ev.target.disabled = true;
    try {
      const r = await api.post('/tasks', { kind, request: text.value || `Recreate pattern ${replayPattern.value}`, project_id: Number(project.value), params: params() });
      await showTask(r.id);
    } catch (e) { fail(e); } finally { ev.target.disabled = false; }
  };
  const taskBtn = h('button', { class: 'btn act', id: 'btn-run-task', title: 'Generate, review, apply with a recovery point, run the tests, fix failures and learn only from verified results',
    onClick: runTask('generate', () => ({ mode: modeSel.value, backend: backend.value, model: modelSel.value || undefined, pattern_ids: picked.size ? [...picked] : undefined })) }, 'Run end-to-end task…');
  const replayTaskBtn = h('button', { class: 'btn secondary act', id: 'btn-replay-task', onClick: runTask('replay', () => ({ pattern_id: Number(replayPattern.value) })) }, 'Recreate and verify…');
  const offlineNote = config.mode !== 'hybrid' ? h('div', { class: 'muted small' }, `Mode is “${config.mode}”: only the local model is used; cloud options are unavailable.`) : null;
  if (config.mode !== 'hybrid') { backend.disabled = true; }
  el.append(
    h('h2', {}, 'Code Generation Workspace'),
    models.reachable ? (models.ready ? h('div', { class: 'ok small', id: 'model-status' }, `Local model ready: ${models.selected}`) : h('div', { class: 'warn small', id: 'model-status' }, `Local model not ready: ${models.reason}`))
      : h('div', { class: 'warn small', id: 'model-status' }, `Local model server unreachable: ${models.reason}`),
    h('div', { class: 'toolbar' }, field('Project', project), field('Local model', modelSel), field('Backend', backend), field('Mode', modeSel)),
    offlineNote, text, h('div', { class: 'row wrap' }, analyze, find, gen, taskBtn), sources,
    h('div', { class: 'row wrap' }, replayPattern, replay, replayTaskBtn),
    h('div', { class: 'muted small' }, '“Generate” and “Plan recreation” only propose changes. “Run end-to-end task” applies them under your permission level, with a recovery point and tests.'),
    out, taskOut);
  const off = await listenForTaskEvents(async (e) => { if (watching && (e.data.task_id === watching || e.data.id === watching)) await showTask(watching); });
  return () => { clearTimeout(timer); watching = null; off(); };
}
