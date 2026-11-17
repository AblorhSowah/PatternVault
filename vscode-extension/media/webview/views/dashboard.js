import { api, host } from '../lib/api.js';
import { h, clear, relTime, badge, emptyState, statusBadge } from '../lib/dom.js';

const MODES = [
  ['learning', 'Learning', 'Observe cloud assistants and capture knowledge'],
  ['offline', 'Offline', 'Local knowledge and local model only'],
  ['hybrid', 'Hybrid', 'Cloud and local per your policy'],
];

function card(title, ...body) {
  return h('section', { class: 'card' }, h('h3', {}, title), ...body);
}

function stat(label, value, id) {
  return h('div', { class: 'stat' }, h('div', { class: 'stat-value', id }, String(value)), h('div', { class: 'stat-label muted' }, label));
}

export async function dashboardView(el, ctx) {
  const [d, kstats, providers, res, usage] = await Promise.all([api.get('/dashboard'), api.get('/knowledge/stats'), api.get('/providers'), api.get('/system/resources'), api.get('/storage')]);
  const lm = d.local_model;
  const fmt = (n) => (n >= 1024 * 1024 * 1024 ? `${(n / 1024 ** 3).toFixed(1)} GB` : `${(n / 1024 ** 2).toFixed(1)} MB`);
  clear(el);
  const modeButtons = MODES.map(([id, label, hint]) => h('button', {
    class: `seg ${d.mode === id ? 'active' : ''}`, title: hint, dataset: { mode: id }, 'aria-pressed': d.mode === id,
    onClick: async () => { await api.put('/mode', { mode: id }); ctx.refresh(); },
  }, label));
  const wsAgents = d.agents_running.length
    ? h('ul', { class: 'list' }, d.agents_running.map((a) => h('li', {}, h('strong', {}, a.agent), ` · ${a.step || 'working'} `, h('span', { class: 'muted' }, relTime(a.started_at)))))
    : h('div', { class: 'muted' }, 'No agents running');
  const stale = kstats.stale_vectors;
  el.append(
    h('h2', {}, 'Dashboard'),
    h('div', { class: 'grid' },
      card('Operating mode', h('div', { class: 'segmented', role: 'group', 'aria-label': 'Operating mode' }, modeButtons),
        h('div', { class: 'muted small' }, `Permissions: ${d.permission_level.replace(/_/g, ' ')} · auto-fallback ${d.auto_fallback ? 'on' : 'off'}`)),
      card('Knowledge', h('div', { class: 'stats' },
        stat('Captured sessions', d.counts.sessions, 'stat-sessions'), stat('Stored patterns', d.counts.patterns, 'stat-patterns'),
        stat('Verified patterns', d.counts.verified_patterns, 'stat-verified'), stat('Generated implementations', d.counts.generated_implementations, 'stat-generated'))),
      card('Local AI model',
        h('div', { id: 'dash-model' }, lm.reachable ? [badge(lm.ready ? 'ready' : 'not ready', lm.ready ? 'ok' : 'warn'), ` Ollama ${lm.version}`] : [badge('unreachable', 'bad'), ' local model server not running']),
        h('div', { class: 'small' }, lm.selected ? `Selected model: ${lm.selected}` : 'No model selected'),
        lm.reason ? h('div', { class: 'warn small' }, lm.reason) : null,
        lm.running.length ? h('div', { class: 'muted small' }, `Loaded: ${lm.running.map((m) => `${m.name} (${fmt(m.size)})`).join(', ')}`) : null),
      card('Resources',
        h('div', { class: 'small', id: 'dash-resources' }, `CPU ${res.cpu_percent.toFixed(0)}% of ${res.cpu_count} cores · RAM ${(res.ram_used_mb / 1024).toFixed(1)} / ${(res.ram_total_mb / 1024).toFixed(1)} GB`),
        h('div', { class: 'small muted' }, `Knowledge DB ${fmt(usage.db_bytes)} · blobs ${fmt(usage.blob_bytes)} · recovery ${fmt(usage.recovery_bytes)}`),
        usage.over_blob_limit || usage.over_db_limit ? h('div', { class: 'warn small' }, 'A storage limit is exceeded; new captures are blocked.') : null),
      card('Embeddings & index',
        h('div', {}, 'Active embedder: ', h('code', {}, kstats.embedder)),
        h('div', { class: 'muted small' }, Object.entries(kstats.vectors).map(([m, n]) => `${m}: ${n} vectors`).join(' · ') || 'no vectors yet'),
        stale ? h('div', { class: 'warn' }, `${stale} pattern(s) need re-indexing `, h('button', { class: 'link', onClick: async () => { await api.post('/knowledge/reindex'); ctx.refresh(); } }, 'Re-index now')) : null),
      card('AI providers', h('ul', { class: 'list' }, providers.map((p) => h('li', {},
        h('strong', {}, p.display_name), ' ',
        p.capabilities.captures_prompts ? badge(p.available ? 'logs found' : 'no logs found', p.available ? 'ok' : 'muted') : badge('workspace capture only', 'muted'),
        p.enabled ? null : badge('disabled', 'bad'))))),
      card('Agent activity', wsAgents),
      card('Recent development history',
        d.recent_tasks.length || d.recent_sessions.length
          ? h('ul', { class: 'list' },
            d.recent_tasks.map((t) => h('li', {}, badge(t.kind), ' ', t.request, ' ', statusBadge(t.verified ? 'verified' : t.status), ' ', h('span', { class: 'muted' }, relTime(t.created_at)))),
            d.recent_sessions.map((s) => h('li', {}, badge('session'), ' ', s.title || `Session ${s.id}`, ' ', badge(s.status, 'muted'), ' ', h('span', { class: 'muted' }, relTime(s.started_at)))))
          : emptyState('Nothing yet', 'Start a capture window or import an assistant session to begin learning.')),
    ),
    h('div', { class: 'muted small' }, `Backend uptime ${Math.round(d.uptime_s)} s`),
    h('div', {}, h('button', { class: 'btn', id: 'btn-capture', onClick: () => host.command('patternvault.startCapture') }, 'Start capture window')),
  );
}
