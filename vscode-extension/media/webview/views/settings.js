import { api, host } from '../lib/api.js';
import { h, clear, badge, relTime, emptyState } from '../lib/dom.js';

const MB = 1024 * 1024;
const fmtBytes = (n) => (n >= MB * 1024 ? `${(n / MB / 1024).toFixed(1)} GB` : `${(n / MB).toFixed(1)} MB`);

function row(label, control, hint) {
  return h('label', { class: 'setting' }, h('span', { class: 'setting-label' }, label), control, hint ? h('span', { class: 'muted small' }, hint) : null);
}

export async function settingsView(el, ctx) {
  const [cfg, models, providers, usage, backups, audit] = await Promise.all([
    api.get('/config'), api.get('/models'), api.get('/providers'), api.get('/storage'), api.get('/backups'), api.get('/audit', { limit: 15 })]);
  clear(el);
  const save = async (patch, ok = 'Saved') => {
    try { await api.patch('/config', patch); ctx.notify(ok); } catch (e) { ctx.notify(e.message, 'error'); }
    ctx.refresh();
  };
  const num = (id, value, patchFn, attrs = {}) => h('input', { type: 'number', id, value: value ?? '', ...attrs, onChange: (e) => { const v = e.target.value === '' ? null : Number(e.target.value); save(patchFn(v)); } });
  const yesno = (value, onChange, id) => h('input', { type: 'checkbox', id, checked: value, onChange: (e) => onChange(e.target.checked) });
  const section = (title, ...body) => h('section', { class: 'card' }, h('h3', {}, title), ...body);

  // ---- operation
  const modeSel = h('select', { id: 's-mode', onChange: (e) => save({ mode: e.target.value }) }, ['learning', 'offline', 'hybrid'].map((m) => h('option', { value: m, selected: cfg.mode === m }, m)));
  const permSel = h('select', { id: 's-permission', onChange: async (e) => {
    if (e.target.value === 'restricted_autonomous' && !(await host.confirm('Restricted autonomous mode lets agents edit project files and run allow-listed test commands without asking each time. Destructive commands stay blocked. Enable?'))) { e.target.value = cfg.permission_level; return; }
    save({ permission_level: e.target.value });
  } }, ['read_only', 'assisted', 'approved_execution', 'restricted_autonomous'].map((m) => h('option', { value: m, selected: cfg.permission_level === m }, m.replace(/_/g, ' '))));

  // ---- local model
  const installed = models.installed.map((m) => m.name);
  const modelSel = h('select', { id: 's-model' }, [h('option', { value: '' }, installed.length ? '(choose a model)' : '(no models installed)'), ...installed.map((n) => h('option', { value: n, selected: cfg.local_model.model === n }, n))]);
  const embedSel = h('select', { id: 's-embedding' }, [h('option', { value: '' }, 'offline hashing embedder (no model)'), ...installed.map((n) => h('option', { value: n, selected: cfg.local_model.embedding_model === n }, n))]);
  const pullName = h('input', { id: 's-pull-name', type: 'text', placeholder: 'e.g. qwen2.5-coder:7b' });
  const modelStatus = models.reachable
    ? h('div', { class: models.ready ? 'ok small' : 'warn small', id: 's-model-status' }, models.ready ? `Ollama ${models.version} · ready: ${models.selected}` : `Ollama ${models.version} reachable · ${models.reason}`)
    : h('div', { class: 'warn small', id: 's-model-status' }, `Ollama not reachable at ${cfg.local_model.base_url}: ${models.reason}`);

  // ---- providers
  const consent = cfg.cloud.consent['openai-compatible'] === true;
  const providerRows = providers.map((p) => h('div', { class: 'row', dataset: { provider: p.key } }, h('label', { class: 'row' }, h('input', { type: 'checkbox', 'aria-label': `Enable ${p.display_name}`, checked: p.enabled, onChange: (e) => save({ providers: { [p.key]: { enabled: e.target.checked } } }) }), h('strong', {}, p.display_name)),
    p.capabilities.captures_prompts ? badge(p.available ? 'logs found' : 'no logs', p.available ? 'ok' : 'muted') : badge('workspace capture only', 'muted')));

  // ---- backups
  const pass = h('input', { id: 's-backup-pass', type: 'password', placeholder: 'passphrase (min 8 characters; empty = unencrypted)', autocomplete: 'new-password' });
  const restorePass = h('input', { id: 's-restore-pass', type: 'password', placeholder: 'passphrase for the selected backup', autocomplete: 'off' });
  const backupBody = (b) => ({ name: b.name, passphrase: restorePass.value || null });

  // ---- audit
  const verifyBtn = h('button', { class: 'btn small secondary', id: 'btn-verify-audit', onClick: async () => { const r = await api.get('/audit/verify'); ctx.notify(r.ok ? `Audit chain intact (${r.checked} entries)` : `AUDIT CHAIN BROKEN at entry ${r.first_bad_id}`, r.ok ? 'ok' : 'error'); ctx.refresh(); } }, 'Verify audit chain');

  el.append(
    h('h2', {}, 'Settings'),
    section('Operation',
      row('Operating mode', modeSel), row('Automatic fallback to offline', yesno(cfg.auto_fallback, (v) => save({ auto_fallback: v }), 's-fallback'), 'When the preferred AI is unreachable, fall back instead of failing.'),
      row('Agent permission level', permSel, 'Destructive commands and writes outside the project are always blocked.')),
    section('Local model',
      modelStatus,
      row('Generation model', h('div', { class: 'row' }, modelSel, h('button', { class: 'btn small', id: 'btn-use-model', onClick: async () => { if (!modelSel.value) return; try { await api.post('/models/select', { model: modelSel.value }); ctx.notify(`Using ${modelSel.value}`); } catch (e) { ctx.notify(e.message, 'error'); } ctx.refresh(); } }, 'Use'),
        h('button', { class: 'btn small secondary', id: 'btn-test-model', onClick: async () => { try { const r = await api.post('/models/test'); ctx.notify(`Model replied “${r.reply}” in ${(r.latency_ms / 1000).toFixed(1)} s`); } catch (e) { ctx.notify(e.message, 'error'); } ctx.refresh(); } }, 'Test'))),
      row('Embedding model', h('div', { class: 'row' }, embedSel, h('button', { class: 'btn small', onClick: async () => { try { await api.post('/models/select', { embedding_model: embedSel.value }); ctx.notify('Embedding model saved (re-index to apply)'); } catch (e) { ctx.notify(e.message, 'error'); } ctx.refresh(); } }, 'Use'))),
      row('Download a model', h('div', { class: 'row' }, pullName, h('button', { class: 'btn small secondary', id: 'btn-pull', onClick: async () => { try { await api.post('/models/pull', { model: pullName.value.trim() }); ctx.notify(`Downloading ${pullName.value.trim()} (see progress in your Ollama logs)`); } catch (e) { ctx.notify(e.message, 'error'); } ctx.refresh(); } }, 'Pull')), 'Downloads from your Ollama server; the model files stay local.'),
      h('h4', {}, 'Resource limits'),
      row('CPU threads', num('s-threads', cfg.local_model.num_thread, (v) => ({ local_model: { num_thread: v } }), { min: 1, max: 512, placeholder: 'auto' }), 'Empty = let Ollama decide.'),
      row('GPU layers', num('s-gpu', cfg.local_model.num_gpu, (v) => ({ local_model: { num_gpu: v } }), { min: 0, max: 999, placeholder: 'auto' }), '0 = CPU only.'),
      row('Context window (tokens)', num('s-ctx', cfg.local_model.num_ctx, (v) => ({ local_model: { num_ctx: v ?? 8192 } }), { min: 512, max: 262144 }), 'Larger = more RAM.'),
      row('Max answer length (tokens)', num('s-maxout', cfg.local_model.max_answer_length, (v) => ({ local_model: { max_answer_length: v ?? 3072 } }), { min: 64, max: 65536 }), 'Stops a model that will not finish. Never more than half the context window.'),
      row('Keep model loaded', h('input', { type: 'text', id: 's-keepalive', value: cfg.local_model.keep_alive, onChange: (e) => save({ local_model: { keep_alive: e.target.value || '5m' } }) }), 'e.g. 5m, 1h, 0 to unload immediately.')),
    section('Provider integrations',
      ...providerRows,
      h('div', { class: 'muted small' }, 'Providers are read from their own local logs only; “workspace capture only” providers expose no supported capture API.'),
      h('h4', {}, 'Cloud generation (hybrid mode only)'),
      row('Allow sending prompts and code to the endpoint below', yesno(consent, (v) => save({ cloud: { consent: { 'openai-compatible': v } } }, v ? 'Cloud consent recorded' : 'Cloud consent withdrawn'), 's-cloud-consent'), 'Off by default. Nothing is sent without this.'),
      row('OpenAI-compatible base URL', h('input', { type: 'text', id: 's-cloud-url', value: cfg.cloud.openai_compatible_base_url || '', placeholder: 'https://…/v1', onChange: (e) => save({ cloud: { openai_compatible_base_url: e.target.value.trim() || null } }) })),
      row('Model name', h('input', { type: 'text', id: 's-cloud-model', value: cfg.cloud.openai_compatible_model || '', onChange: (e) => save({ cloud: { openai_compatible_model: e.target.value.trim() || null } }) })),
      h('div', { class: 'row' }, h('button', { class: 'btn small secondary', id: 'btn-set-key', onClick: () => host.command('patternvault.setProviderKey') }, 'Store API key in VS Code SecretStorage…'), h('span', { class: 'muted small' }, 'Keys are never written to PatternVault’s files.'))),
    section('Privacy',
      row('Capture AI sessions', yesno(cfg.privacy.capture_enabled, (v) => save({ privacy: { capture_enabled: v } }), 's-capture')),
      row('Allow retrieval across projects', yesno(cfg.privacy.allow_cross_project_retrieval, (v) => save({ privacy: { allow_cross_project_retrieval: v } }), 's-cross')),
      row('Additional excluded paths', h('textarea', { id: 's-exclude', rows: 4, placeholder: 'one glob per line, e.g. legacy/**', onChange: (e) => save({ privacy: { exclude_globs: e.target.value.split('\n').map((s) => s.trim()).filter(Boolean) } }) }, cfg.privacy.exclude_globs.join('\n')), 'Always excluded: .env, private keys, credentials, binaries. Secrets are always redacted before storage.'),
      h('div', {}, badge('secret redaction: always on', 'ok'))),
    section('Agents',
      row('Correction attempts per task', num('s-fix', cfg.agents.max_fix_attempts, (v) => ({ agents: { max_fix_attempts: v ?? 3 } }), { min: 0, max: 10 }), 'After this many failed fixes the project is restored to its recovery point.'),
      row('Parallel steps', num('s-parallel', cfg.agents.max_parallel_steps, (v) => ({ agents: { max_parallel_steps: v ?? 4 } }), { min: 1, max: 16 }))),
    section('Knowledge & storage',
      h('div', { class: 'small', id: 's-usage' }, `Database ${fmtBytes(usage.db_bytes)} / ${usage.limits.max_db_mb} MB · blobs ${fmtBytes(usage.blob_bytes)} / ${usage.limits.max_blobs_mb} MB · vectors ${fmtBytes(usage.vector_bytes)} · backups ${fmtBytes(usage.backup_bytes)} · recovery ${fmtBytes(usage.recovery_bytes)}`),
      usage.over_blob_limit || usage.over_db_limit ? h('div', { class: 'error', role: 'alert' }, 'A storage limit is exceeded: new captures are blocked until you free space or raise the limit.') : null,
      row('Database limit (MB)', num('s-max-db', cfg.storage.max_db_mb, (v) => ({ storage: { max_db_mb: v ?? 4096 } }), { min: 16 })),
      row('Blob storage limit (MB)', num('s-max-blobs', cfg.storage.max_blobs_mb, (v) => ({ storage: { max_blobs_mb: v ?? 2048 } }), { min: 16 })),
      row('Vector backend', h('code', {}, cfg.storage.vector_backend)),
      h('div', { class: 'row wrap' },
        h('button', { class: 'btn', id: 'btn-export', onClick: async () => { const data = await api.get('/knowledge/export'); await host.saveJson('patternvault-export.json', data); } }, 'Export knowledge…'),
        h('button', { class: 'btn secondary', id: 'btn-reindex', onClick: async () => { const r = await api.post('/knowledge/reindex'); ctx.notify(`Re-indexed ${r.indexed} pattern(s)`); ctx.refresh(); } }, 'Re-index'),
        h('button', { class: 'btn secondary', id: 'btn-gc', onClick: async () => { const r = await api.post('/storage/gc'); ctx.notify(`Removed ${r.removed} unreferenced blob(s), freed ${fmtBytes(r.freed_bytes)}`); ctx.refresh(); } }, 'Free unused storage'),
        h('button', { class: 'btn secondary', id: 'btn-quality', onClick: async () => { const r = await api.post('/knowledge/quality'); ctx.notify(`${r.duplicates.length} duplicate(s), ${r.conflicts.length} conflict(s), ${r.outdated.length} outdated, ${r.recommendations.length} recommendation(s)`); ctx.refresh(); } }, 'Analyze quality'),
        h('button', { class: 'btn secondary', id: 'btn-clear-chats', title: 'Permanently delete every saved chat conversation', onClick: async () => { try { const r = await api.del('/chat/conversations'); ctx.notify(`Deleted ${r.deleted} chat conversation(s)`); } catch (e) { if (e.status !== 499) ctx.notify(e.message, 'error'); } ctx.refresh(); } }, 'Delete all chats…'))),
    section('Backup and restore',
      h('div', { class: 'muted small' }, 'Backups contain your whole knowledge base. Encrypt them: a lost passphrase cannot be recovered.'),
      h('div', { class: 'row wrap' }, pass, h('button', { class: 'btn', id: 'btn-backup', onClick: async () => { try { const r = await api.post('/backups', { passphrase: pass.value || null }); ctx.notify(`Backup created: ${r.name} (${r.encrypted ? 'encrypted' : 'NOT encrypted'})`); } catch (e) { ctx.notify(e.message, 'error'); } ctx.refresh(); } }, 'Create backup')),
      backups.length ? h('div', { class: 'stack', id: 's-backups' }, restorePass, ...backups.slice(0, 8).map((b) => h('div', { class: 'row wrap', dataset: { backup: b.name } }, h('code', {}, b.name), badge(b.encrypted ? 'encrypted' : 'plain', b.encrypted ? 'ok' : 'warn'), h('span', { class: 'muted small' }, `${fmtBytes(b.bytes)} · ${relTime(b.created_at)}`),
        h('button', { class: 'btn small secondary', onClick: async () => { try { const m = await api.post('/backups/verify', backupBody(b)); ctx.notify(`Backup OK: ${m.counts.knowledge_patterns} patterns, ${m.counts.dev_sessions} sessions`); } catch (e) { ctx.notify(e.message, 'error'); } ctx.refresh(); } }, 'Verify'),
        h('button', { class: 'btn small', onClick: async () => { try { const r = await api.post('/backups/restore', { ...backupBody(b), confirm: true }); ctx.notify(`Restored ${r.restored}; safety copy ${r.safety_copy}`); } catch (e) { ctx.notify(e.message, 'error'); } ctx.refresh(); } }, 'Restore…'),
        h('button', { class: 'btn small danger', onClick: async () => { try { await api.del(`/backups/${b.name}`, { confirm: true }); ctx.notify('Backup deleted'); } catch (e) { ctx.notify(e.message, 'error'); } ctx.refresh(); } }, 'Delete')))) : emptyState('No backups yet')),
    section('Audit log', h('div', { class: 'row' }, verifyBtn),
      h('div', { class: 'table-wrap' }, h('table', { class: 'table', id: 's-audit' }, h('thead', {}, h('tr', {}, ['When', 'Actor', 'Action', 'Outcome'].map((t) => h('th', {}, t)))),
        h('tbody', {}, audit.map((a) => h('tr', {}, h('td', { class: 'muted' }, relTime(a.ts)), h('td', {}, a.actor), h('td', {}, a.action), h('td', {}, badge(a.outcome, a.outcome === 'allowed' ? 'muted' : 'bad')))))))),
  );
}
