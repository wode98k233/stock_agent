/* ── api ── */

async function loadFromAPI() {
  try {
    const res = await fetch(`${S.apiBase}/api/runs`);
    S.runs = await res.json();
    render();
  } catch(e) { console.error('API load failed:', e); }
}

async function loadRunDetailAPI(runId) {
  try {
    const [runRes, stepsRes] = await Promise.all([
      fetch(`${S.apiBase}/api/runs/${runId}`),
      fetch(`${S.apiBase}/api/runs/${runId}/steps`),
    ]);
    const run = await runRes.json();
    const steps = await stepsRes.json();
    run.steps = steps;
    return run;
  } catch(e) { console.error('API detail failed:', e); return null; }
}

function loadFromJSON(data) {
  S.runs = data.runs || [data.run];
  render();
}

function onFilePick(e) {
  const file = e.target.files[0];
  if (file) readFile(file);
  e.target.value = '';
}

function readFile(file) {
  const reader = new FileReader();
  reader.onload = () => {
    try { loadFromJSON(JSON.parse(reader.result)); }
    catch(e) { alert('JSON 解析失败: ' + e.message); }
  };
  reader.readAsText(file);
}

function setupDnD() {
  const overlay = document.getElementById('dropOverlay');
  let dragCount = 0;
  document.addEventListener('dragenter', e => { e.preventDefault(); dragCount++; overlay.classList.add('visible'); });
  document.addEventListener('dragleave', e => { e.preventDefault(); dragCount--; if (dragCount <= 0) { overlay.classList.remove('visible'); dragCount = 0; } });
  document.addEventListener('dragover', e => e.preventDefault());
  document.addEventListener('drop', e => {
    e.preventDefault(); dragCount = 0; overlay.classList.remove('visible');
    const file = e.dataTransfer.files[0];
    if (file && file.name.endsWith('.json')) readFile(file);
  });
}

function setupSearch() {
  let timer;
  document.getElementById('searchInput').addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => render(), 200);
  });
}

function getFilteredRuns() {
  const q = (document.getElementById('searchInput').value || '').toLowerCase();
  if (!q) return S.runs;
  return S.runs.filter(r => JSON.stringify(r).toLowerCase().includes(q));
}
