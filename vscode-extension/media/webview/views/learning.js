import { api, host } from '../lib/api.js';
import { h, clear, badge, relTime, emptyState, statusBadge } from '../lib/dom.js';

function capabilityBadges(c) {
  return [
    c.captures_prompts ? badge('prompts', 'ok') : badge('no prompts', 'muted'),
    c.captures_responses ? badge('responses', 'ok') : null,
    c.captures_tool_calls ? badge('tool calls', 'ok') : null,
    c.captures_file_edits ? badge('file edits', 'ok') : badge('no file edits', 'muted'),
    c.verified_against_real_install ? null : badge('unverified against real install', 'warn'),
  ];
}

async function providerSection(projects, onChange, ctx) {
  const providers = await api.get('/providers');
  const box = h('div', { class: 'stack' });
  for (const p of providers) {
    const row = h('div', { class: 'card inner', dataset: { provider: p.key } },
      h('div', { class: 'row' }, h('strong', {}, p.display_name), p.available ? badge('log source found', 'ok') : badge('log source not found', 'muted')),
      h('div', { class: 'row wrap' }, ...capabilityBadges(p.capabilities)),
      h('div', { class: 'muted small' }, p.capabilities.mechanism),
      p.capabilities.notes ? h('div', { class: 'muted small' }, p.capabilities.notes) : null);
    if (p.available && p.capabilities.captures_prompts && p.key !== 'manual' && projects.length) {
      const select = h('select', { 'aria-label': `Project for ${p.display_name}` }, projects.map((pr) => h('option', { value: pr.id }, pr.name)));
      const list = h('div', { class: 'stack' });
      const find = h('button', { class: 'btn', onClick: async () => {
        clear(list);
        try {
          const found = await api.get(`/providers/${p.key}/sessions`, { project_id: select.value });
          if (!found.length) list.append(emptyState('No sessions for this project'));
          for (const s of found) {
            list.append(h('div', { class: 'row' }, h('span', {}, s.title || s.external_id), h('span', { class: 'muted small' }, `${s.message_count} messages · ${relTime(s.started_at)}`),
              h('button', { class: 'btn small', onClick: async (e) => {
                e.target.disabled = true;
                try {
                  const r = await api.post(`/providers/${p.key}/import`, { project_id: Number(select.value), external_id: s.external_id });
                  ctx.notify(`Imported “${r.title || s.title || s.external_id}”: ${r.file_changes} file change(s), ${(r.patterns || []).length} pattern(s) learned. Review them below.`);
                  onChange();
                } catch (err) { e.target.textContent = err.message; e.target.disabled = false; }
              } }, 'Import')));
          }
        } catch (err) { list.append(h('div', { class: 'error' }, err.message)); }
      } }, 'Find sessions');
      row.append(h('div', { class: 'row' }, select, find), list);
    }
    box.append(row);
  }
  return box;
}

async function reviewQueue(onChange) {
  const res = await api.get('/patterns', { status: 'observed', limit: 20 });
  const gen = await api.get('/patterns', { status: 'generated', limit: 20 });
  const items = [...res.items, ...gen.items];
  if (!items.length) return emptyState('Nothing awaiting review', 'Newly learned patterns appear here. They are never trusted until you approve them and tests verify them.');
  return h('div', { class: 'stack' }, items.map((p) => h('div', { class: 'card inner', dataset: { patternId: p.id } },
    h('div', { class: 'row' }, h('strong', {}, p.title), statusBadge(p.status), badge(p.category), p.language ? badge(p.language, 'muted') : null),
    h('div', { class: 'muted small' }, p.summary),
    h('div', { class: 'row' },
      h('button', { class: 'btn small', onClick: async () => { await api.post(`/patterns/${p.id}/approve`); onChange(); } }, 'Approve'),
      h('button', { class: 'btn small secondary', onClick: async () => { if (await host.confirm(`Deprecate "${p.title}"? It will no longer be suggested.`)) { await api.post(`/patterns/${p.id}/deprecate`); onChange(); } } }, 'Reject'),
      p.provenance?.observed_test_pass ? badge('assistant ran passing tests (not verification)', 'muted') : null))));
}

async function sessionList() {
  const s = await api.get('/sessions', { limit: 25 });
  if (!s.items.length) return emptyState('No captured sessions yet');
  return h('div', { class: 'table-wrap' }, h('table', { class: 'table' },
    h('thead', {}, h('tr', {}, ['Title', 'Source', 'Status', 'Files', 'Patterns', 'When'].map((t) => h('th', {}, t)))),
    h('tbody', {}, s.items.map((x) => h('tr', {},
      h('td', {}, x.title || `Session ${x.id}`), h('td', {}, x.provider || x.capture_source), h('td', {}, badge(x.status, 'muted'), x.outcome !== 'unknown' ? badge(`observed ${x.outcome}`, x.outcome === 'passed' ? 'ok' : 'bad') : null),
      h('td', {}, String(x.file_changes)), h('td', {}, String(x.extracted_pattern_ids.length)), h('td', { class: 'muted' }, relTime(x.started_at)))))));
}

export async function learningView(el, ctx) {
  const projects = await api.get('/projects');
  clear(el);
  const refresh = () => ctx.refresh();
  el.append(
    h('h2', {}, 'AI Learning Centre'),
    h('p', { class: 'muted' }, 'PatternVault learns from the work of AI coding assistants using only supported, local sources. Anything learned starts as “observed” and must be reviewed.'),
    h('h3', {}, 'Supported AI providers'), await providerSection(projects, refresh, ctx),
    h('h3', {}, 'Patterns awaiting review'), await reviewQueue(refresh),
    h('h3', {}, 'Captured sessions & learning history'), await sessionList(),
    h('div', { class: 'row' },
      h('button', { class: 'btn', id: 'btn-start-capture', onClick: () => host.command('patternvault.startCapture') }, 'Start capture window'),
      h('button', { class: 'btn secondary', onClick: () => host.command('patternvault.endCapture') }, 'End capture and learn')),
  );
}
