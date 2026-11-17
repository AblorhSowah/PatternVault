// A deliberately small Markdown subset for chat answers: paragraphs, headings, bullet/numbered lists, fenced code,
// `inline code`, **bold** and [#12] pattern citations. PURE (no DOM, no imports) so it can be unit-tested in Node.
//
// Safety: answers can be shaped by untrusted pattern text, so nothing here ever produces HTML or a clickable link. The DOM
// layer (render.js) turns this tree into text nodes only.

const FENCE_OPEN = /^ {0,3}(`{3,}|~{3,})\s*([A-Za-z0-9_+#.-]*)[^\n]*$/;
const HEADING = /^(#{1,4})\s+(.*)$/;
const BULLET = /^\s*[-*+]\s+(.*)$/;
const NUMBERED = /^\s*\d+[.)]\s+(.*)$/;
const INLINE = /(`+)([^\n]+?)\1(?!`)|\*\*([^*\n]+?)\*\*|\[#(\d{1,9})\]/g;

/** @returns {{t: 'text'|'code'|'strong'|'cite', v?: string, id?: number}[]} */
export function parseInline(text) {
  const out = [];
  let last = 0;
  for (const m of String(text).matchAll(INLINE)) {
    if (m.index > last) out.push({ t: 'text', v: text.slice(last, m.index) });
    if (m[2] !== undefined) out.push({ t: 'code', v: m[2] });
    else if (m[3] !== undefined) out.push({ t: 'strong', v: m[3] });
    else out.push({ t: 'cite', id: Number(m[4]) });
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push({ t: 'text', v: text.slice(last) });
  return out;
}

const isBlockStart = (line) => FENCE_OPEN.test(line) || HEADING.test(line) || BULLET.test(line) || NUMBERED.test(line);

/**
 * @returns {({type: 'p', inline: object[]} | {type: 'h', level: number, inline: object[]} | {type: 'ul' | 'ol', items: object[][]}
 *   | {type: 'code', lang: string, text: string, closed: boolean})[]}
 * An unterminated code fence is returned with `closed: false`, so a half-streamed answer still renders sensibly.
 */
export function parseMarkdown(src) {
  const lines = String(src ?? '').replace(/\r\n?/g, '\n').split('\n');
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = FENCE_OPEN.exec(line);
    if (fence) {
      const marker = fence[1];
      const body = [];
      let closed = false;
      i += 1;
      while (i < lines.length) {
        const t = lines[i].trim();
        if (t.length >= marker.length && [...t].every((c) => c === marker[0])) { closed = true; i += 1; break; }
        body.push(lines[i]);
        i += 1;
      }
      blocks.push({ type: 'code', lang: fence[2].toLowerCase(), text: body.join('\n'), closed });
      continue;
    }
    if (line.trim() === '') { i += 1; continue; }
    const heading = HEADING.exec(line);
    if (heading) { blocks.push({ type: 'h', level: heading[1].length, inline: parseInline(heading[2]) }); i += 1; continue; }
    const listRe = BULLET.test(line) ? BULLET : NUMBERED.test(line) ? NUMBERED : null;
    if (listRe) {
      const items = [];
      while (i < lines.length && listRe.test(lines[i])) { items.push(parseInline(listRe.exec(lines[i])[1])); i += 1; }
      blocks.push({ type: listRe === BULLET ? 'ul' : 'ol', items });
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() !== '' && !(para.length && isBlockStart(lines[i]))) { para.push(lines[i]); i += 1; }
    blocks.push({ type: 'p', inline: parseInline(para.join('\n')) });
  }
  return blocks;
}

/** Pattern ids cited as [#12] anywhere in the text outside code blocks, in first-seen order. */
export function citedIds(src) {
  const ids = [];
  const visit = (tokens) => { for (const t of tokens) if (t.t === 'cite' && !ids.includes(t.id)) ids.push(t.id); };
  for (const b of parseMarkdown(src)) {
    if (b.inline) visit(b.inline);
    if (b.items) b.items.forEach(visit);
  }
  return ids;
}
