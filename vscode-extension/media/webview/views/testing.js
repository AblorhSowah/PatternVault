import { api } from '../lib/api.js';
import { h, clear, badge, relTime, emptyState } from '../lib/dom.js';
import { statusKind } from '../lib/tasks.js';

const AGENT_LABEL = {
  'unit-testing': 'Unit', 'integration-testing': 'Integration', 'e2e-testing': 'End-to-end', 'api-testing': 'API', 'database-testing': 'Database',
  'extension-testing': 'Extension', 'security-testing': 'Security', 'performance-testing': 'Performance', 'regression-testing': 'Regression', 'code-quality': 'Code quality',
};

function resultRow(r) {
  return h('details', { class: `result-row ${r.status}`, open: r.status === 'failed' || r.status === 'error', dataset: { status: r.status } },
    h('summary', {}, badge(r.status, statusKind(r.status === 'passed' ? 'success' : r.status === 'error' ? 'failed' : r.status)), ' ', h('code', {}, r.name),
      r.correction_applied ? badge(`corrected · retest ${r.retest_status || '?'}`, r.retest_status === 'passed' ? 'ok' : 'warn') : null),
    h('dl', { class: 'kv' }, h('dt', {}, 'Description'), h('dd', {}, r.description || '—'), h('dt', {}, 'Expected'), h('dd', {}, r.expected || '—'), h('dt', {}, 'Actual'), h('dd', {}, r.actual || '—'),
      r.correction_applied ? [h('dt', {}, 'Correction applied'), h('dd', {}, r.correction_applied), h('dt', {}, 'Retest'), h('dd', {}, r.retest_status || '—')] : null),
    r.error_details ? h('pre', { class: 'code' }, h('code', {}, r.error_details)) : null);
}

export async function testingView(el, ctx) {
  const [summary, sessions, projects, agentsList, recentTasks] = await Promise.all([api.get('/testing/summary'), api.get('/testing/sessions', { limit: 40 }), api.get('/projects'), api.get('/agents'), api.get('/tasks', { limit: 5 })]);
  clear(el);
  const testingAgents = agentsList.filter((a) => a.category === 'testing');
  const project = h('select', { id: 't-project' }, projects.map((p) => h('option', { value: p.id }, p.name)));
  const picked = new Set(['unit-testing']);
  const fix = h('input', { type: 'checkbox', id: 't-fix' });
  const detail = h('div', { id: 'session-detail', class: 'stack' }, emptyState('Select a test session', 'Pick a run to see every test with expected vs actual results.'));
  const filter = { agent: '', status: '' };
  const list = h('div', { class: 'stack', id: 'session-list' });

  const drawList = () => {
    clear(list);
    const rows = sessions.filter((s) => (!filter.agent || s.agent_key === filter.agent) && (!filter.status || s.status === filter.status));
    if (!rows.length) { list.append(emptyState('No test sessions match')); return; }
    for (const s of rows) {
      list.append(h('button', { class: 'result', dataset: { sessionId: s.id }, onClick: async () => showSession(s.id) },
        h('div', { class: 'row' }, h('strong', {}, AGENT_LABEL[s.agent_key] || s.agent_key), badge(s.status, statusKind(s.status === 'passed' ? 'success' : s.status === 'skipped' ? 'skipped' : 'failed')), h('span', { class: 'muted small' }, relTime(s.started_at))),
        h('div', { class: 'small muted' }, `${s.summary.passed ?? 0} passed · ${s.summary.failed ?? 0} failed · ${s.summary.skipped ?? 0} skipped${s.summary.skipped_reason ? ` — ${s.summary.skipped_reason}` : ''}`)));
    }
  };
  const showSession = async (id) => {
    const d = await api.get(`/testing/sessions/${id}`);
    const findings = d.summary.findings || [];
    clear(detail);
    detail.append(
      h('div', { class: 'row wrap' }, h('h3', { id: 'detail-agent' }, AGENT_LABEL[d.agent_key] || d.agent_key), badge(d.status, statusKind(d.status === 'passed' ? 'success' : d.status === 'skipped' ? 'skipped' : 'failed')),
        d.coverage_percent != null ? badge(`coverage ${d.coverage_percent}%`, 'info') : null, h('span', { class: 'muted small' }, `${(d.summary.duration_s ?? 0)} s`)),
      h('div', { class: 'muted small' }, h('code', {}, d.command || '')),
      d.summary.error ? h('div', { class: 'error', role: 'alert' }, d.summary.error) : null,
      d.summary.skipped_reason ? h('div', { class: 'note' }, `Skipped (this is not a pass): ${d.summary.skipped_reason}`) : null,
      d.summary.regressions?.length ? h('div', { class: 'error' }, `Regressions: ${d.summary.regressions.join(', ')}`) : null,
      d.summary.metrics ? h('div', { class: 'small' }, Object.entries(d.summary.metrics).map(([k, v]) => `${k}: ${typeof v === 'number' ? v.toFixed(1) : v}`).join(' · ')) : null,
      findings.length ? h('div', {}, h('strong', {}, `Security findings (${findings.length})`), h('ul', { class: 'list' }, findings.slice(0, 40).map((f) => h('li', {}, badge(f.severity, f.severity === 'critical' || f.severity === 'high' ? 'bad' : 'warn'), ' ', h('code', {}, `${f.path}:${f.line}`), ` ${f.title} (${f.cwe}) — ${f.fix}`)))) : null,
      ...d.results.map(resultRow));
  };

  const failing = summary.failing_results_total;
  el.append(
    h('h2', {}, 'Testing Centre'),
    h('p', { class: 'muted' }, 'A task is only “verified” when tests actually ran and passed. Tests that could not run are shown as skipped, never as passed.'),
    h('div', { class: 'grid', id: 'agent-cards' }, summary.agents.map((a) => h('section', { class: 'card', dataset: { testAgent: a.agent } },
      h('div', { class: 'row' }, h('strong', {}, AGENT_LABEL[a.agent] || a.agent), a.last ? badge(a.last.status, statusKind(a.last.status === 'passed' ? 'success' : a.last.status === 'skipped' ? 'skipped' : 'failed')) : badge('never run', 'muted')),
      a.last ? h('div', { class: 'small muted' }, `${a.last.summary.passed ?? 0} passed · ${a.last.summary.failed ?? 0} failed · ${relTime(a.last.started_at)}`) : null))),
    failing ? h('div', { class: 'error', id: 'failing-banner' }, `${failing} failing test result(s) recorded`) : null,
    h('section', { class: 'card' }, h('h3', {}, 'Run tests'),
      h('div', { class: 'toolbar' }, h('label', { class: 'field' }, h('span', { class: 'muted small' }, 'Project'), project)),
      h('div', { class: 'row wrap', id: 't-agents' }, testingAgents.map((a) => h('label', { class: 'row small' }, h('input', { type: 'checkbox', dataset: { agent: a.key }, checked: picked.has(a.key), disabled: !a.enabled, onChange: (e) => (e.target.checked ? picked.add(a.key) : picked.delete(a.key)) }), AGENT_LABEL[a.key] || a.key))),
      h('label', { class: 'row small' }, fix, 'If tests fail, let the debugging agent propose a correction and re-test (needs the local model)'),
      h('div', { class: 'row' }, h('button', { class: 'btn', id: 'btn-run-tests', onClick: async (ev) => {
        if (!project.value || !picked.size) { ctx.notify('Choose a project and at least one agent.', 'error'); ctx.refresh(); return; }
        ev.target.disabled = true;
        try { const r = await api.post('/testing/run', { project_id: Number(project.value), agents: [...picked], fix: fix.checked }); ctx.notify(`Test task ${r.id} started`); } catch (e) { ctx.notify(e.message, 'error'); }
        ctx.refresh();
      } }, 'Run selected agents'))),
    h('div', { class: 'toolbar' },
      h('label', { class: 'field' }, h('span', { class: 'muted small' }, 'Agent'), h('select', { id: 't-filter-agent', onChange: (e) => { filter.agent = e.target.value; drawList(); } }, [h('option', { value: '' }, 'All agents'), ...Object.entries(AGENT_LABEL).map(([k, v]) => h('option', { value: k }, v))])),
      h('label', { class: 'field' }, h('span', { class: 'muted small' }, 'Status'), h('select', { id: 't-filter-status', onChange: (e) => { filter.status = e.target.value; drawList(); } }, ['', 'passed', 'failed', 'error', 'skipped'].map((s) => h('option', { value: s }, s || 'All statuses'))))),
    h('div', { class: 'split' }, list, detail));
  drawList();
  const onEvent = (e) => { if (e.detail.type === 'testing.report' || e.detail.type === 'task.finished') ctx.refresh(); };
  document.addEventListener('pv:event', onEvent);
  // Poll while a test task is in flight: pushed events can be missed, and results would otherwise appear stale.
  const active = recentTasks.some((t) => ['running', 'pending', 'awaiting_approval'].includes(t.status));
  const timer = active ? setTimeout(() => ctx.refresh(), 2000) : null;
  return () => { clearTimeout(timer); document.removeEventListener('pv:event', onEvent); };
}
