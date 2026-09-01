/* ── state ── */

const TYPE_ICONS = {
  llm:'🧠', tool:'🔧', chain:'🔗', retriever:'📄',
  agent:'🤖', tools:'🔧', planner:'📋', executor:'⚡',
  replanner:'🔄', classifier:'🏷️', graph:'🌳',
  unified_executor:'🚀', observer:'👁️', adjuster:'🔧', reviewer:'📝',
  undefined:'❓',
};

const S = { runs: [], selectedId: null, apiBase: null, selectedStep: null, drawerTab: 'overview' };

if (window.location.protocol !== 'file:') {
  S.apiBase = window.location.origin;
}

let _stepIdx = 0;
let _stepsByIdx = [];
let _paraMap = {};
let _paraIdx = 0;
let _curLlmStepIdx = -1;
let _llmIdxMap = {};

document.addEventListener('DOMContentLoaded', () => {
  if (S.apiBase) loadFromAPI();
  setupDnD();
  setupSearch();
  document.getElementById('fileInput').addEventListener('change', onFilePick);
  document.getElementById('clearBtn').addEventListener('click', () => {
    S.runs = []; S.selectedId = null; render();
  });
  document.getElementById('drawerClose').addEventListener('click', closeDrawer);
  document.querySelectorAll('.drawer-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.drawer-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      S.drawerTab = tab.dataset.tab;
      renderDrawerContent();
    });
  });
});
