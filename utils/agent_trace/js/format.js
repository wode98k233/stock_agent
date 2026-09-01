/* ── format ── */

function contentFingerprint(text, len) {
  if (!text) return '';
  const s = String(text).trim();
  const skip = 80;
  const mid = s.slice(skip, skip + (len || 200));
  const tail = s.slice(-50);
  return mid + '|' + tail + '|' + s.length;
}

function isJsonParagraph(text) {
  const s = text.trim();
  if (!s.startsWith('{') && !s.startsWith('[')) return false;
  try { JSON.parse(s); return true; } catch { return false; }
}

function getWithinMsgDups(content) {
  if (!content || content.length < 100) return new Set();
  const paras = content.split(/\n{2,}/).map(p => p.trim()).filter(p => p.length > 20);
  const seen = new Map();
  const dups = new Set();
  for (const p of paras) {
    if (isJsonParagraph(p)) continue;
    const fp = contentFingerprint(p, 200);
    if (seen.has(fp)) dups.add(fp);
    else seen.set(fp, true);
  }
  return dups;
}

function renderFormattedContent(raw) {
  if (!raw) return '';
  const str = typeof raw === 'object' ? JSON.stringify(raw, null, 2) : String(raw);
  const jsonResult = tryFormatJSON(str);
  if (jsonResult) return jsonResult;
  const fencedJsonResult = tryFormatFencedJSON(str);
  if (fencedJsonResult) return fencedJsonResult;
  if (hasMarkdown(str)) return formatMarkdown(str);
  return `<div class="fmt-block">
    <div class="fmt-header raw" onclick="toggleFmt(this)">TEXT <span class="fmt-toggle">▼</span></div>
    <div class="fmt-body">${esc(str)}</div>
  </div>`;
}

function tryFormatFencedJSON(str) {
  const trimmed = str.trim();
  const match = trimmed.match(/^```\s*([A-Za-z0-9_-]*)[^\S\r\n]*(?:\r?\n)?([\s\S]*?)\s*```$/);
  if (!match) return null;
  const lang = (match[1] || '').toLowerCase();
  if (lang && lang !== 'json') return null;
  return tryFormatJSON(match[2].trim());
}

function tryFormatJSON(str) {
  const trimmed = str.trim();
  if (trimmed.length < 2) return null;
  const first = trimmed[0];
  if (first !== '{' && first !== '[' && first !== '"') return null;
  let obj;
  try { obj = JSON.parse(trimmed); } catch(e) { return null; }
  const formatted = syntaxHighlightJSON(JSON.stringify(obj, null, 2));
  return `<div class="fmt-block">
    <div class="fmt-header json" onclick="toggleFmt(this)">JSON <span class="fmt-toggle">▼</span></div>
    <div class="fmt-body"><pre style="margin:0;white-space:pre-wrap;word-break:break-word">${formatted}</pre></div>
  </div>`;
}

function syntaxHighlightJSON(json) {
  return esc(json).replace(
    /("(\\u[\da-fA-F]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g,
    match => {
      let cls = 'json-num';
      if (/^"/.test(match)) cls = /:$/.test(match) ? 'json-key' : 'json-str';
      else if (/true|false/.test(match)) cls = 'json-bool';
      else if (/null/.test(match)) cls = 'json-null';
      return `<span class="${cls}">${match}</span>`;
    }
  );
}

function hasMarkdown(str) {
  let score = 0;
  if (/^#{1,6}\s/m.test(str)) score += 2;
  if (/\*\*[^*]+\*\*/.test(str)) score += 1;
  if (/^\s*[-*]\s/m.test(str)) score += 1;
  if (/^\s*\d+\.\s/m.test(str)) score += 1;
  if (/```/.test(str)) score += 2;
  if (/`[^`]+`/.test(str)) score += 1;
  if (/^---/m.test(str)) score += 1;
  return score >= 2;
}

function renderMarkdownCodeBlock(codeBuf, codeFenceLang) {
  const codeText = codeBuf.join('\n');
  if ((codeFenceLang || '').toLowerCase() === 'json') {
    const jsonResult = tryFormatJSON(codeText);
    if (jsonResult) return jsonResult;
  }
  return `<div class="md-codeblock">${esc(codeText)}</div>`;
}

function formatMarkdown(str) {
  const lines = str.split('\n');
  let html = '', inList = false, inCodeBlock = false, codeBuf = [], codeFenceLang = '';
  for (const line of lines) {
    if (line.trim().startsWith('```')) {
      if (inCodeBlock) {
        html += renderMarkdownCodeBlock(codeBuf, codeFenceLang);
        codeBuf = [];
        codeFenceLang = '';
        inCodeBlock = false;
      } else {
        const fenceMatch = line.trim().match(/^```\s*([A-Za-z0-9_-]*)/);
        codeFenceLang = fenceMatch ? fenceMatch[1].toLowerCase() : '';
        inCodeBlock = true;
      }
      continue;
    }
    if (inCodeBlock) { codeBuf.push(line); continue; }
    const hMatch = line.match(/^(#{1,6})\s+(.*)/);
    if (hMatch) {
      if (inList) { html += '</div>'; inList = false; }
      html += `<div class="md-heading h${hMatch[1].length}">${esc(hMatch[2])}</div>`;
      continue;
    }
    const liMatch = line.match(/^\s*[-*]\s+(.*)/);
    if (liMatch) {
      if (!inList) { html += '<div class="md-list">'; inList = true; }
      html += `<div>• ${inlineFormat(liMatch[1])}</div>`;
      continue;
    }
    const olMatch = line.match(/^\s*(\d+)\.\s+(.*)/);
    if (olMatch) {
      if (!inList) { html += '<div class="md-list">'; inList = true; }
      html += `<div>${olMatch[1]}. ${inlineFormat(olMatch[2])}</div>`;
      continue;
    }
    if (inList) { html += '</div>'; inList = false; }
    if (/^---+\s*$/.test(line)) { html += '<hr style="border:none;border-top:1px solid var(--border);margin:4px 0">'; continue; }
    if (!line.trim()) { html += '<div style="height:3px"></div>'; continue; }
    html += `<div>${inlineFormat(line)}</div>`;
  }
  if (inList) html += '</div>';
  if (inCodeBlock) html += renderMarkdownCodeBlock(codeBuf, codeFenceLang);
  return `<div class="fmt-block">
    <div class="fmt-header md" onclick="toggleFmt(this)">MARKDOWN <span class="fmt-toggle">▼</span></div>
    <div class="fmt-body">${html}</div>
  </div>`;
}

function inlineFormat(str) {
  return esc(str)
    .replace(/\*\*([^*]+)\*\*/g, '<span class="md-bold">$1</span>')
    .replace(/`([^`]+)`/g, '<span class="md-code">$1</span>');
}

function toggleFmt(header) {
  const body = header.nextElementSibling;
  body.classList.toggle('collapsed');
  header.querySelector('.fmt-toggle').textContent = body.classList.contains('collapsed') ? '▶' : '▼';
}

function renderNumberedParagraphs(text, llmStepIdx, withinDups) {
  if (!text || text.length < 30) return renderFormattedContent(text);
  const paras = text.split(/\n{2,}/).map(p => p.trim()).filter(p => p.length > 0);
  if (paras.length < 2) return renderFormattedContent(text);

  const wsDups = withinDups || new Set();
  const isMarkdown = hasMarkdown(text);
  let html = '<div class="fmt-block">';
  html += `<div class="fmt-header ${isMarkdown ? 'md' : 'raw'}" onclick="toggleFmt(this)">${isMarkdown ? 'MARKDOWN' : 'TEXT'} <span class="fmt-toggle">▼</span></div>`;
  html += '<div class="fmt-body">';

  for (let i = 0; i < paras.length; i++) {
    const p = paras[i];
    const pNum = _paraIdx++;
    const skipFp = isJsonParagraph(p);
    const fp = (!skipFp && p.length > 20) ? contentFingerprint(p, 200) : null;
    const info = fp ? _paraMap[fp] : null;

    const crossCount = info ? info.stepIndices.filter(si => si <= llmStepIdx).length : 0;
    const isCrossDup = crossCount > 1;
    const isWithinDup = fp && wsDups.has(fp);

    let numLabel;
    if (isCrossDup && isWithinDup) {
      const tipSteps = info ? info.stepLabels.slice(0, 5).map(l => `<div class="para-dup-tooltip-step">${esc(l)}</div>`).join('') : '';
      numLabel = `<span class="para-num para-dup para-dup-marker">${pNum + 1}<span class="para-dup-count">×${crossCount}</span><span class="para-dup-tooltip"><div class="para-dup-tooltip-title">出现在以下步骤</div>${tipSteps}</span></span>`;
    } else if (isCrossDup) {
      const tipSteps = info ? info.stepLabels.slice(0, 5).map(l => `<div class="para-dup-tooltip-step">${esc(l)}</div>`).join('') : '';
      numLabel = `<span class="para-num para-dup para-dup-marker">${pNum + 1}<span class="para-dup-count">×${crossCount}</span><span class="para-dup-tooltip"><div class="para-dup-tooltip-title">出现在以下步骤</div>${tipSteps}</span></span>`;
    } else if (isWithinDup) {
      numLabel = `<span class="para-num para-ws-dup" title="步骤内重复">${pNum + 1}</span>`;
    } else {
      numLabel = `<span class="para-num">${pNum + 1}</span>`;
    }

    const lineClass = isCrossDup ? 'para-line-dup' : (isWithinDup ? 'para-line-ws-dup' : '');
    const content = isMarkdown ? inlineFormat(p) : esc(p);
    html += `<div class="para-line ${lineClass}">${numLabel}<span class="para-text">${content}</span></div>`;
  }

  html += '</div></div>';
  return html;
}
