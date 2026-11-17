import { api, host } from './api.js';
import { h, badge, relTime, emptyState } from './dom.js';

const STATUS_KIND = { verified: 'ok', success: 'ok', completed: 'info', running: 'info', applying: 'info', pending: 'muted', awaiting_approval: 'warn', skipped: 'muted', blocked: 'warn', failed: 'bad', cancelled: 'muted' };
export const statusKind = (s) => STATUS_KIND[s] || 'muted';

export function planView(task) {
  return h('ol', { class: 'plan' }, task.plan.map((s) => h('li', { class: 'plan-step', dataset: { step: s.key, status: s.status } },
    badge(s.status, statusKind(s.status)), ' ', h('strong', {}, s.key), s.agent ? h('span', { class: 'muted small' }, ` · ${s.agent}`) : null,
    s.summary ? h('div', { class: 'muted small' }, s.summary) : null)));
}

export function executionsTable(task) {
  if (!task.executions.length) return h('div', { class: 'muted small' }, 'No agent executions yet.');
  return h('div', { class: 'table-wrap' }, h('table', { class: 'table' },
    h('thead', {}, h('tr', {}, ['Agent', 'Step', 'Attempt', 'Status', 'Time', 'Summary'].map((t) => h('th', {}, t)))),
    h('tbody', {}, task.executions.map((e) => h('tr', {}, h('td', {}, e.agent), h('td', {}, h('code', {}, e.step)), h('td', {}, String(e.attempt)),
      h('td', {}, badge(e.status, statusKind(e.status))), h('td', {}, e.duration_ms == null ? '…' : `${(e.duration_ms / 1000).toFixed(1)} s`), h('td', {}, e.summary || e.error || ''))))));
}

export function approvalCard(a, onDone) {
  const decide = (approve) => async (ev) => {
    ev.target.disabled = true;
    try { await api.post(`/approvals/${a.id}/decide`, { approve, note: '' }); } catch (e) { ev.target.textContent = e.message; return; }
    onDone();
  };
  return h('div', { class: 'card inner approval', dataset: { approvalId: a.id } },
    h('div', { class: 'row wrap' }, badge('approval needed', 'warn'), badge(a.action, 'muted'), h('strong', {}, a.summary)),
    a.detail?.preview ? h('pre', { class: 'code' }, h('code', {}, a.detail.preview)) : null,
    a.detail?.command ? h('pre', { class: 'code' }, h('code', {}, a.detail.command)) : null,
    h('div', { class: 'row' }, h('button', { class: 'btn small', onClick: decide(true) }, 'Approve'), h('button', { class: 'btn small secondary', onClick: decide(false) }, 'Deny')));
}

export async function taskPanel(task, onChange, { showReport = true } = {}) {
  const approvals = (await api.get('/approvals', { pending_only: true })).filter((a) => a.task_id === task.id);
  const files = task.result?.files_applied || [];
  return h('div', { class: 'card inner task-card', dataset: { taskId: task.id } },
    h('div', { class: 'row wrap' }, h('strong', {}, `Task ${task.id}`), badge(task.kind), badge(task.status, statusKind(task.status)), task.verified ? badge('verified by passing tests', 'ok') : badge('not verified', 'muted'),
      h('span', { class: 'muted small' }, `${task.permission_level.replace(/_/g, ' ')} · ${task.mode}`), h('span', { class: 'muted small' }, relTime(task.created_at))),
    h('div', {}, task.request),
    task.error ? h('div', { class: 'error', role: 'alert' }, task.error) : null,
    ...approvals.map((a) => approvalCard(a, onChange)),
    planView(task),
    task.result?.rolled_back ? h('div', { class: 'note' }, `Verification failed after ${task.result.fix_attempts} correction attempt(s): the project was restored to recovery point #${task.result.recovery_point_id}.`) : null,
    task.result?.gate ? h('div', { class: 'small' }, `Verification gate — passed: ${task.result.gate.passed.join(', ') || 'none'} · failed: ${task.result.gate.failed.join(', ') || 'none'} · skipped: ${task.result.gate.skipped.join(', ') || 'none'}`) : null,
    files.length ? h('div', { class: 'small' }, h('strong', {}, 'Files changed: '), files.map((f) => h('code', { class: 'chip' }, f))) : null,
    h('details', {}, h('summary', {}, 'Agent execution history'), executionsTable(task)),
    h('div', { class: 'row' },
      ['running', 'pending', 'awaiting_approval'].includes(task.status) ? h('button', { class: 'btn small secondary', onClick: async () => { await api.post(`/tasks/${task.id}/cancel`); onChange(); } }, 'Cancel') : null,
      showReport ? h('button', { class: 'btn small secondary', onClick: async (ev) => { const md = await api.get(`/tasks/${task.id}/report`); ev.target.after(h('pre', { class: 'code' }, h('code', {}, typeof md === 'string' ? md : JSON.stringify(md)))); ev.target.remove(); } }, 'Show report') : null));
}

export async function listenForTaskEvents(handler) {
  const fn = (e) => { if (/^(task|approval|testing|files)\./.test(e.detail.type)) handler(e.detail); };
  document.addEventListener('pv:event', fn);
  return () => document.removeEventListener('pv:event', fn);
}

export { host, emptyState };
