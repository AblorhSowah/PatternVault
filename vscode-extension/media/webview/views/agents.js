import { api, host } from '../lib/api.js';
import { h, clear, badge, relTime, emptyState } from '../lib/dom.js';
import { taskPanel, listenForTaskEvents, statusKind } from '../lib/tasks.js';

const CATEGORY = { development: 'Development agents', testing: 'Testing agents', orchestration: 'Orchestration' };

export async function agentsView(el, ctx) {
  const [agents, tasks, approvals, points] = await Promise.all([api.get('/agents'), api.get('/tasks', { limit: 15 }), api.get('/approvals', { pending_only: true }), api.get('/recovery-points')]);
  clear(el);
  const refresh = () => ctx.refresh();
  const groups = {};
  for (const a of agents) (groups[a.category] ||= []).push(a);
  const agentTable = (list) => h('div', { class: 'table-wrap' }, h('table', { class: 'table', dataset: { category: list[0].category } },
    h('thead', {}, h('tr', {}, ['Agent', 'Status', 'Enabled', 'Last execution', 'Permissions'].map((t) => h('th', {}, t)))),
    h('tbody', {}, list.map((a) => h('tr', { dataset: { agent: a.key } },
      h('td', {}, h('strong', {}, a.name), h('div', { class: 'muted small' }, a.description)),
      h('td', {}, badge(a.status, a.status === 'running' ? 'info' : 'muted')),
      h('td', {}, a.key === 'orchestrator' ? badge('always on', 'muted') : h('input', { type: 'checkbox', 'aria-label': `Enable ${a.name}`, checked: a.enabled, onChange: async (e) => { try { await api.patch(`/agents/${a.key}`, { enabled: e.target.checked }); ctx.notify(`${a.name} ${e.target.checked ? 'enabled' : 'disabled'}`); } catch (err) { ctx.notify(err.message, 'error'); } refresh(); } })),
      h('td', {}, a.last_execution ? [badge(a.last_execution.status, statusKind(a.last_execution.status)), ` ${a.last_execution.step} · ${relTime(a.last_execution.at)}`] : h('span', { class: 'muted' }, 'never')),
      h('td', { class: 'muted small' }, a.permissions.join(', ') || '—'))))));
  const taskCards = [];
  for (const t of tasks) taskCards.push(await taskPanel(await api.get(`/tasks/${t.id}`), refresh));
  el.append(
    h('h2', {}, 'Agent Control Centre'),
    h('p', { class: 'muted' }, 'Every agent action is permission-gated, audited and recorded here. Disabled agents are skipped by the orchestrator.'),
    approvals.length ? h('section', { id: 'pending-approvals' }, h('h3', {}, `Pending approvals (${approvals.length})`), ...approvals.map((a) => h('div', { class: 'card inner approval', dataset: { approvalId: a.id } },
      h('div', { class: 'row wrap' }, badge(a.action, 'warn'), h('strong', {}, a.summary)),
      a.detail?.preview ? h('pre', { class: 'code' }, h('code', {}, a.detail.preview)) : null,
      h('div', { class: 'row' }, h('button', { class: 'btn small', onClick: async () => { await api.post(`/approvals/${a.id}/decide`, { approve: true, note: '' }); refresh(); } }, 'Approve'),
        h('button', { class: 'btn small secondary', onClick: async () => { await api.post(`/approvals/${a.id}/decide`, { approve: false, note: '' }); refresh(); } }, 'Deny'))))) : null,
    ...Object.entries(CATEGORY).filter(([k]) => groups[k]).flatMap(([k, title]) => [h('h3', {}, title), agentTable(groups[k])]),
    h('h3', {}, 'Task history'),
    tasks.length ? h('div', { class: 'stack', id: 'task-history' }, taskCards) : emptyState('No tasks yet', 'Run a generation, replay or test task to see the agents work.'),
    h('h3', {}, 'Recovery points'),
    points.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'table', id: 'recovery-table' }, h('thead', {}, h('tr', {}, ['#', 'Label', 'Files', 'Status', 'When', ''].map((t) => h('th', {}, t)))),
      h('tbody', {}, points.slice(0, 15).map((p) => h('tr', {}, h('td', {}, String(p.id)), h('td', {}, p.label || `task ${p.task_id ?? ''}`), h('td', {}, p.files.join(', ')), h('td', {}, badge(p.status, p.status === 'active' ? 'info' : 'muted')), h('td', { class: 'muted' }, relTime(p.created_at)),
        h('td', {}, p.status === 'expired' ? null : h('button', { class: 'btn small secondary', onClick: async () => { if (await host.confirm(`Restore ${p.files.length} file(s) to how they were before recovery point #${p.id}? Current content of those files will be replaced.`)) { try { await api.post(`/recovery-points/${p.id}/restore`, undefined, { confirm: true }); ctx.notify(`Restored recovery point #${p.id}`); } catch (e) { ctx.notify(e.message, 'error'); } refresh(); } } }, 'Restore'))))))) : emptyState('No recovery points', 'A recovery point is created before any agent modifies your files.'),
  );
  const off = await listenForTaskEvents(() => refresh());
  const active = tasks.some((t) => ['running', 'pending', 'awaiting_approval'].includes(t.status)) || approvals.length > 0;
  const timer = active ? setTimeout(refresh, 2500) : null;  // poll while work is in flight
  return () => { clearTimeout(timer); off(); };
}
