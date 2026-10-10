/* ── AI 评审（run 级） ── */
let _reviewRunId = null;
let _reviewTimer = null;
let _reviewError = null;

function openReview(runId) {
  if (!S.apiBase) {
    alert('AI 评审需要在 API 模式下使用（通过 launcher.py trace 启动）');
    return;
  }
  _reviewRunId = runId;
  _reviewError = null;
  const box = document.getElementById('reviewDrawer');
  if (box) box.classList.add('open');
  renderReviewBody({ items: [] }, true);
  refreshReviews();
}

function closeReview() {
  if (_reviewTimer) { clearInterval(_reviewTimer); _reviewTimer = null; }
  _reviewRunId = null;
  _reviewError = null;
  const box = document.getElementById('reviewDrawer');
  if (box) box.classList.remove('open');
}

async function submitReview() {
  if (!_reviewRunId) return;
  const btn = document.getElementById('reviewSubmitBtn');
  if (btn) { btn.disabled = true; btn.textContent = '评审中...'; }
  const res = await createReview(_reviewRunId);
  _reviewError = (res && res.error) ? res.error : null;
  await refreshReviews();
}

async function refreshReviews() {
  const rid = _reviewRunId;
  if (!rid) return;
  const data = await fetchReviews(rid);
  if (rid !== _reviewRunId) return;
  const items = (data && data.items) || [];
  renderReviewBody({ items, error: _reviewError });
  if (_reviewTimer) { clearInterval(_reviewTimer); _reviewTimer = null; }
  if (items.some(it => it.status === 'running')) {
    _reviewTimer = setInterval(refreshReviews, 3000);
  }
}

function renderReviewBody(data, loading) {
  const body = document.getElementById('reviewBody');
  if (!body) return;
  const items = data.items || [];
  let html = `<div class="review-actions">`
    + `<button class="btn" id="reviewSubmitBtn" onclick="submitReview()">`
    + `${items.length ? '重新评审' : '开始评审'}</button></div>`;
  if (data.error) {
    html += `<div class="review-error-banner">${esc(data.error)}</div>`;
  }
  if (!items.length) {
    html += `<div class="review-empty">${loading ? '加载中...' : (data.error ? '' : '暂无评审记录')}</div>`;
    body.innerHTML = html;
    return;
  }
  for (const it of items) html += renderReviewItem(it);
  body.innerHTML = html;
}

function renderReviewItem(it) {
  const meta = `v${esc(it.rubric_version || '?')} · ${esc(it.judge_model || '')} · ${esc((it.created_at || '').slice(0, 19))}`;
  if (it.status === 'running') {
    return `<div class="review-card"><div class="review-card-head">`
      + `<span class="badge badge-running">running</span>`
      + `<span class="review-meta">${meta}</span></div>`
      + `<div class="review-empty">评审进行中...</div></div>`;
  }
  if (it.status === 'failed') {
    return `<div class="review-card"><div class="review-card-head">`
      + `<span class="badge badge-error">failed</span>`
      + `<span class="review-meta">${meta}</span></div>`
      + `<div class="review-error">${esc(it.error || '')}</div></div>`;
  }
  const dims = (it.scores && typeof it.scores === 'object' && !Array.isArray(it.scores))
    ? it.scores : {};
  const dimHtml = Object.keys(dims)
    .map(k => `<span class="review-dim">${esc(k)}: ${dims[k]}</span>`).join('');
  const findings = (Array.isArray(it.findings) ? it.findings : []).map(f => {
    const issues = Array.isArray(f.issues) ? f.issues : [];
    const suggestions = Array.isArray(f.suggestions) ? f.suggestions : [];
    return `
    <div class="review-finding">
      <div class="review-finding-head">#${f.step_ref || 0} · ${esc(f.dimension || '')} · ${esc(f.verdict || '')}</div>
      ${issues.length ? `<div class="review-issue">问题：${issues.map(esc).join('；')}</div>` : ''}
      ${suggestions.length ? `<div class="review-suggestion">建议：${suggestions.map(esc).join('；')}</div>` : ''}
    </div>`;
  }).join('');
  return `
    <div class="review-card">
      <div class="review-card-head">
        <span class="badge badge-success">done</span>
        <span class="review-score">${it.overall == null ? '-' : it.overall}</span>
        <span class="review-meta">${meta}</span>
      </div>
      <div class="review-dims">${dimHtml}</div>
      ${findings ? `<div class="review-findings">${findings}</div>` : ''}
    </div>`;
}
