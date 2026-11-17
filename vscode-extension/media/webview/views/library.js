import { api, host } from '../lib/api.js';
import { h, clear, badge, statusBadge, confidenceBar, relTime, debounce, emptyState } from '../lib/dom.js';

const STATUSES = ['', 'observed', 'generated', 'approved', 'tested', 'verified', 'deprecated'];

function select(id, label, options, value, onChange) {
  return h('label', { class: 'field' }, h('span', { class: 'muted small' }, label),
    h('select', { id, onChange: (e) => onChange(e.target.value) },
      options.map(([v, text]) => h('option', { value: v, selected: v === value }, text))));
}

function facetOptions(label, counts) {
  return [['', `All ${label}`], ...Object.entries(counts || {}).sort().map(([k, n]) => [k, `${k} (${n})`])];
}

function fileView(f) {
  return h('details', { class: 'file' },
    h('summary', {}, h('code', {}, f.path), ' ', badge(f.role, 'muted'), f.language ? badge(f.language, 'muted') : null, f.truncated ? badge('truncated', 'warn') : null),
    h('pre', { class: 'code' }, h('code', {}, f.content)));
}

async function detail(box, id, ctx, reload) {
  clear(box);
  box.append(h('div', { class: 'muted' }, 'Loading…'));
  const [p, versions] = await Promise.all([api.get(`/patterns/${id}`), api.get(`/patterns/${id}/versions`)]);
  clear(box);
  const act = (label, fn, cls = 'btn small') => h('button', { class: cls, onClick: async () => { try { await fn(); await reload(); await detail(box, id, ctx, reload); } catch (e) { alert_(box, e.message); } } }, label);
  const c = p.content || {};
  box.append(
    h('div', { class: 'row' }, h('h3', { id: 'detail-title' }, p.title), statusBadge(p.status), badge(p.category)),
    h('div', { class: 'row' }, confidenceBar(p.confidence), h('span', { class: 'muted small' }, `confidence ${(p.confidence * 100).toFixed(0)}% · used ${p.use_count}× (${p.success_count} ok / ${p.failure_count} failed)`)),
    h('p', {}, p.summary),
    h('div', { class: 'meta muted small' }, [p.language, p.framework && `${p.framework}${p.framework_version ? ' ' + p.framework_version : ''}`, `v${p.version}`, p.provenance?.provider && `learned from ${p.provenance.provider}`, p.provenance?.imported && 'imported', relTime(p.created_at)].filter(Boolean).join(' · ')),
    p.provenance?.category_evidence?.length ? h('div', { class: 'muted small' }, `Why “${p.category}”: ${p.provenance.category_evidence.join('; ')}`) : null,
    h('div', { class: 'row wrap' },
      p.status !== 'deprecated' && p.status !== 'verified' ? act(p.user_approved ? 'Approved ✓' : 'Approve', () => api.post(`/patterns/${id}/approve`)) : null,
      p.status === 'deprecated' ? act('Restore', () => api.post(`/patterns/${id}/restore`)) : act('Deprecate', async () => { if (await host.confirm(`Deprecate "${p.title}"?`)) await api.post(`/patterns/${id}/deprecate`); else throw new Error('cancelled'); }, 'btn small secondary'),
      act('👍', () => api.post(`/patterns/${id}/feedback`, { rating: 1 }), 'btn small secondary'),
      act('👎', () => api.post(`/patterns/${id}/feedback`, { rating: -1 }), 'btn small secondary'),
      h('button', { class: 'btn small danger', id: 'btn-delete', onClick: async () => { if (await host.confirm(`Permanently delete "${p.title}" and its history? This cannot be undone.`)) { await api.del(`/patterns/${id}`, { confirm: true }); clear(box); await reload(); } } }, 'Delete…')),
    p.status !== 'verified' ? h('div', { class: 'note' }, p.status === 'tested' ? 'Tested, but needs your approval (or a successful reuse) to become verified.' : 'Not verified: review the code before relying on it. Verification requires a passing test run.') : null,
    c.dependencies?.length ? h('div', {}, h('strong', {}, 'Dependencies: '), c.dependencies.map((d) => h('code', { class: 'chip' }, `${d.name} ${d.version || ''}`.trim()))) : null,
    c.steps?.length ? h('div', {}, h('strong', {}, 'Workflow'), h('ol', {}, c.steps.map((s) => h('li', {}, s)))) : null,
    h('div', {}, h('strong', {}, `Files (${(c.files || []).length})`), ...(c.files || []).map(fileView)),
    h('div', {}, h('strong', {}, 'Versions'), h('ul', { class: 'list' }, versions.map((v) => h('li', {}, `v${v.version} · ${v.change_note || 'initial'} · `, h('span', { class: 'muted' }, relTime(v.created_at)))))),
  );
}

function alert_(box, msg) {
  box.prepend(h('div', { class: 'error', role: 'alert' }, msg));
}

export async function libraryView(el, ctx) {
  const facets = await api.get('/patterns/facets');
  const filt = { q: '', category: '', language: '', framework: '', status: '' };
  clear(el);
  const results = h('div', { class: 'results', id: 'results', role: 'list' });
  const detailBox = h('div', { class: 'detail', id: 'detail' }, emptyState('Select a pattern', 'Pick a result to inspect its code, versions and evidence.'));
  const total = h('div', { class: 'muted small', id: 'result-count' });
  let selected = null;

  async function load() {
    const q = Object.fromEntries(Object.entries(filt).filter(([, v]) => v));
    q.limit = 100;
    const res = await api.get('/patterns', q);
    clear(results);
    total.textContent = `${res.total} pattern(s)`;
    if (!res.items.length) results.append(emptyState('No patterns match', 'Capture or import a session to start building the library.'));
    for (const p of res.items) {
      results.append(h('button', { class: `result ${p.id === selected ? 'active' : ''}`, role: 'listitem', dataset: { patternId: p.id }, onClick: async () => { selected = p.id; await load(); await detail(detailBox, p.id, ctx, load); } },
        h('div', { class: 'row' }, h('strong', {}, p.title), statusBadge(p.status)),
        h('div', { class: 'row small muted' }, badge(p.category, 'muted'), p.language || '', p.framework ? `· ${p.framework}${p.framework_version ? ' ' + p.framework_version : ''}` : '', `· ${p.file_count} file(s)`),
        confidenceBar(p.confidence)));
    }
  }
  const reload = debounce(load, 200);
  el.append(
    h('h2', {}, 'Pattern Library'),
    h('div', { class: 'toolbar' },
      h('input', { id: 'search', type: 'search', placeholder: 'Search by meaning, file, symbol or route…', 'aria-label': 'Search patterns', onInput: (e) => { filt.q = e.target.value; reload(); } }),
      select('f-category', 'Category', facetOptions('categories', facets.categories), '', (v) => { filt.category = v; load(); }),
      select('f-language', 'Language', facetOptions('languages', facets.languages), '', (v) => { filt.language = v; load(); }),
      select('f-framework', 'Framework', facetOptions('frameworks', facets.frameworks), '', (v) => { filt.framework = v; load(); }),
      select('f-status', 'Status', STATUSES.map((s) => [s, s || 'All statuses']), '', (v) => { filt.status = v; load(); })),
    total,
    h('div', { class: 'split' }, results, detailBox),
  );
  await load();
}
