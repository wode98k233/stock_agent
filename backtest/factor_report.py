"""因子研究报告生成：单文件 HTML（内联 ECharts，零 CDN，可离线分发）。

设计基线（2026-10-16 设计师冻结版，勿改）
------------------------------------------
- 浅色主题（项目 ``variables.css`` 为 ``color-scheme: light``，深色是另立工作量）
- Lucide 内联 SVG 描边图标，尺寸 14/16/20，**零 emoji**
- 信息架构：L0 KPI + 显著性条 → L1 IC 分析 → L2 分层回测 → L3 稳健性 → L4 口径摘要
- 分层色阶用**顺序型**（teal→amber 5 级，表达「顺序」）；红绿只表达「方向」
- 分层标签中性：只写 Q1..Q5，多空写「Q5−Q1」，**不写多头/空头层**
- 缺失值断线，不归零
"""
from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

ECHARTS_REL = Path("server/static/vendor/echarts.min.js")


def _load_echarts() -> str:
    path = Path(__file__).resolve().parents[1] / ECHARTS_REL
    if path.exists():
        return path.read_text(encoding="utf-8", errors="ignore")
    return ""


def _esc(s) -> str:
    return html.escape("" if s is None else str(s))


def _fmt(v, digits: int = 3, pct: bool = False, signed: bool = False) -> str:
    if v is None or (isinstance(v, float) and v != v):
        return "—"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "—"
    if pct:
        x *= 100.0
    sign = "+" if signed and x > 0 else ""
    return f"{sign}{x:.{digits}f}{'%' if pct else ''}"


_ICON = {
    "ranking": '<path d="M3 3v18h18"/><path d="M7 15l3-4 3 2 4-6"/>',
    "layers": '<path d="M12 2 2 7l10 5 10-5-10-5Z"/><path d="m2 17 10 5 10-5"/><path d="m2 12 10 5 10-5"/>',
    "activity": '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    "calendar": '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
    "trending-down": '<path d="m22 17-8.5-8.5-5 5L2 7"/><path d="M16 17h6v-6"/>',
    "arrows": '<path d="m21 16-4 4-4-4"/><path d="M17 20V4"/><path d="m3 8 4-4 4 4"/><path d="M7 4v16"/>',
    "chart-column": '<path d="M3 3v18h18"/><rect x="7" y="10" width="3" height="7"/><rect x="12" y="6" width="3" height="11"/><rect x="17" y="13" width="3" height="4"/>',
    "shield": '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1Z"/>',
    "alert": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    "info": '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
}


def _icon(name: str, size: int = 16) -> str:
    body = _ICON.get(name, _ICON["info"])
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
            f'stroke-linejoin="round" aria-hidden="true">{body}</svg>')


_CSS = """
:root{
  --bg:#f6f7f9; --panel:#ffffff; --border:#e5e7eb; --border-light:#eef0f3;
  --text:#1f2937; --text-secondary:#6b7280; --muted:#9ca3af;
  --brand:#176b5b; --accent:#8f8e88; --strategy-accent:#c2c1ba;
  --up:#e74c3c; --down:#176b5b;
  --fr-up:var(--up); --fr-down:var(--down);
  --fr-q1:#dcebe8; --fr-q2:#8fc0b5; --fr-q3:#3f8f7c; --fr-q4:#8a6a1f; --fr-q5:#b45309;
  --font:-apple-system,"Microsoft YaHei","PingFang SC",sans-serif;
  --font-mono:"SF Mono","Cascadia Code","Fira Code",Consolas,monospace;
  --radius:10px; --shadow:0 1px 2px rgba(16,24,40,.05);
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:var(--font);
  font-size:14px;line-height:1.6;-webkit-font-smoothing:antialiased}
.fr-wrap{max-width:1240px;margin:0 auto;padding:28px 20px 64px}
.fr-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;
  flex-wrap:wrap;padding-bottom:16px;border-bottom:1px solid var(--border)}
.fr-head h1{margin:0 0 6px;font-size:21px;font-weight:650;letter-spacing:-.01em}
.fr-head .fr-meta{color:var(--text-secondary);font-size:12.5px;font-family:var(--font-mono)}
.fr-badge{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;
  border:1px solid var(--border);border-radius:999px;background:var(--panel);
  color:var(--text-secondary);font-size:12px}
.fr-kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin:20px 0 12px}
.fr-kpi{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);
  padding:12px 14px;box-shadow:var(--shadow)}
.fr-kpi .label{color:var(--text-secondary);font-size:12px;margin-bottom:6px}
.fr-kpi .value{font-family:var(--font-mono);font-size:19px;font-weight:600;letter-spacing:-.02em}
.fr-kpi .sub{color:var(--muted);font-size:11.5px;margin-top:4px;font-family:var(--font-mono)}
.fr-kpi .value.positive{color:var(--up)} .fr-kpi .value.negative{color:var(--down)}
.fr-sig{display:flex;flex-wrap:wrap;align-items:center;gap:10px 18px;
  background:var(--panel);border:1px solid var(--border);border-left:0;border-radius:var(--radius);
  padding:10px 14px;font-size:12.5px;color:var(--text-secondary);box-shadow:var(--shadow)}
.fr-sig b{color:var(--text);font-family:var(--font-mono);font-weight:600}
.fr-section{margin-top:26px}
.fr-section>h2{display:flex;align-items:center;gap:8px;margin:0 0 12px;
  font-size:15px;font-weight:600;color:var(--text)}
.fr-section>h2 svg{color:var(--brand);flex:none}
.fr-card{background:var(--panel);border:1px solid var(--border);
  border-radius:var(--radius);box-shadow:var(--shadow);overflow:hidden}
.fr-table{width:100%;border-collapse:collapse;font-size:12.5px}
.fr-table th{background:#fafbfc;color:var(--text-secondary);font-weight:600;
  text-align:right;padding:9px 10px;border-bottom:1px solid var(--border);white-space:nowrap}
.fr-table th:first-child,.fr-table th:nth-child(2),.fr-table th:nth-child(3){text-align:left}
.fr-table td{padding:8px 10px;border-bottom:1px solid var(--border-light);
  text-align:right;font-family:var(--font-mono);white-space:nowrap}
.fr-table td:first-child,.fr-table td:nth-child(2),.fr-table td:nth-child(3){
  text-align:left;font-family:var(--font)}
.fr-table tr:last-child td{border-bottom:0}
.fr-table tbody tr:hover{background:#fafbfc}
.fr-table .pos{color:var(--up)} .fr-table .neg{color:var(--down)}
.fr-table .rank{color:var(--muted);font-family:var(--font-mono)}
.fr-cat{display:inline-block;padding:1px 7px;border-radius:5px;background:#f1f5f4;
  color:var(--brand);font-size:11.5px}
.fr-pass{display:inline-block;padding:1px 7px;border-radius:5px;font-size:11.5px}
.fr-pass.yes{background:rgba(231,76,60,.09);color:var(--up)}
.fr-pass.no{background:#f3f4f6;color:var(--muted)}
.fr-factor{border-bottom:1px solid var(--border-light)}
.fr-factor:last-child{border-bottom:0}
.fr-factor summary{display:flex;align-items:center;gap:10px;padding:11px 14px;
  cursor:pointer;list-style:none;font-size:13px}
.fr-factor summary::-webkit-details-marker{display:none}
.fr-factor summary:hover{background:#fafbfc}
.fr-factor summary .name{font-weight:600}
.fr-factor summary .id{color:var(--muted);font-family:var(--font-mono);font-size:12px}
.fr-factor summary .kv{margin-left:auto;color:var(--text-secondary);
  font-family:var(--font-mono);font-size:12px}
.fr-detail{padding:4px 14px 18px}
.fr-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.fr-chart-box{border:1px solid var(--border-light);border-radius:8px;padding:10px 12px}
.fr-chart-title{display:flex;align-items:center;gap:6px;color:var(--text-secondary);
  font-size:12.5px;margin-bottom:6px}
.fr-chart-title svg{color:var(--accent);flex:none}
.fr-chart{height:240px;width:100%}
.fr-chart.tall{height:280px}
.fr-note{color:var(--muted);font-size:12px;margin:8px 0 0}
.fr-foot{margin-top:30px;padding-top:14px;border-top:1px solid var(--border);
  color:var(--muted);font-size:12px}
.fr-summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px 24px;
  padding:14px}
.fr-summary .k{color:var(--text-secondary);font-size:12px}
.fr-summary .v{font-family:var(--font-mono);font-size:12.5px}
.fr-caveat{display:flex;gap:8px;padding:10px 14px;color:var(--text-secondary);
  font-size:12.5px;border-top:1px solid var(--border-light)}
.fr-caveat svg{color:#b45309;flex:none;margin-top:2px}
@media (max-width:1100px){
  .fr-kpis{grid-template-columns:repeat(3,1fr)}
  .fr-grid{grid-template-columns:1fr}
  .fr-summary{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media (max-width:640px){
  .fr-kpis{grid-template-columns:repeat(2,1fr)}
  .fr-wrap{padding:18px 12px 48px}
  .fr-summary{grid-template-columns:1fr}
}
"""

_JS = r"""
const P = window.__FACTOR_PAYLOAD__;
const CSSV = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const Q_COLORS = () => [CSSV('--fr-q1'), CSSV('--fr-q2'), CSSV('--fr-q3'), CSSV('--fr-q4'), CSSV('--fr-q5')];
const UP = () => CSSV('--fr-up'), DOWN = () => CSSV('--fr-down');
const GRID = () => ({ left: 44, right: 16, top: 26, bottom: 30 });
const AXIS = () => ({
  axisLine: { lineStyle: { color: '#e5e7eb' } },
  axisTick: { show: false },
  axisLabel: { color: '#6b7280', fontSize: 10, fontFamily: 'monospace' },
  splitLine: { lineStyle: { color: '#f1f3f5' } }
});
const TOOLTIP = () => ({
  trigger: 'axis', backgroundColor: '#fff', borderColor: '#e5e7eb',
  textStyle: { color: '#1f2937', fontSize: 11 },
  axisPointer: { lineStyle: { color: '#cbd5e1' } }
});
const charts = {};
function mount(id, option) {
  const el = document.getElementById(id);
  if (!el || charts[id]) return;
  const c = echarts.init(el, null, { renderer: 'canvas' });
  c.setOption(option);
  charts[id] = c;
  return c;
}

function signColor(v) { return (v || 0) >= 0 ? UP() : DOWN(); }

function renderIC(f, box) {
  const d = f.ic_series;
  mount(box + '_ic', {
    tooltip: TOOLTIP(), grid: GRID(),
    legend: { top: 0, right: 0, itemWidth: 10, itemHeight: 8, textStyle: { fontSize: 10, color: '#6b7280' } },
    xAxis: { type: 'category', data: d.dates, ...AXIS(), splitLine: { show: false } },
    yAxis: { type: 'value', ...AXIS() },
    series: d.series.map((s, i) => ({
      name: s.name, type: 'line', data: s.data, showSymbol: false, connectNulls: false,
      lineStyle: { width: i === 2 ? 1.4 : 1, type: i === 2 ? 'dashed' : 'solid' },
      itemStyle: { color: [CSSV('--accent'), CSSV('--strategy-accent'), CSSV('--brand')][i] },
      markLine: i === 0 ? { silent: true, symbol: 'none',
        lineStyle: { color: '#cbd5e1', type: 'dashed', width: 1 },
        label: { show: false }, data: [{ yAxis: 0 }] } : undefined
    }))
  });
}

/* IC 分布：齿状直方图（借 lieflat F14 的语法）
   一箱一把梯子、一档 = 一个观测：小箱里的 1~2 个观测在柱状图里是贴地的一条缝，
   在梯子里看得见；箱界画在梯脚之间，提醒这是连续区间而不是类目。 */
function renderHist(f, box) {
  const h = f.ic_hist;
  if (!h || !h.edges || !h.edges.length) return;
  const el = document.getElementById(box + '_hist');
  if (!el || el.dataset.done) return;
  el.dataset.done = '1';

  const counts = h.counts, edges = h.edges, n = counts.length;
  const total = counts.reduce((a, b) => a + b, 0) || 1;
  const W = 900, H = 236, X0 = 50, BASE = 192, TOP = 20;
  const PW = (W - X0 - 26) / n;
  const peak = Math.max(...counts);
  const step = Math.min(6.0, (BASE - TOP) / Math.max(peak, 1));
  const HW = Math.min(PW * 0.34, 10);
  const center = i => (edges[i] + edges[i + 1]) / 2;

  let s = '';
  /* IC = 0 的零轴：正负方向的分界 */
  for (let i = 0; i < n; i++) {
    if (edges[i] <= 0 && edges[i + 1] > 0) {
      const zx = X0 + i * PW + (0 - edges[i]) / (edges[i + 1] - edges[i]) * PW;
      s += `<line x1="${zx.toFixed(1)}" y1="${TOP - 10}" x2="${zx.toFixed(1)}" y2="${BASE + 4}"
        stroke="#8f8e88" stroke-width="1" stroke-dasharray="3 4"/>`;
      s += `<text x="${zx.toFixed(1)}" y="${TOP - 14}" font-size="9" fill="#8f8e88"
        text-anchor="middle">IC = 0</text>`;
      break;
    }
  }
  /* 梯子：一档一条线 */
  counts.forEach((v, i) => {
    const cx = X0 + i * PW + PW / 2;
    const col = center(i) >= 0 ? UP() : DOWN();
    for (let k = 0; k < v; k++) {
      const y = BASE - k * step;
      const w = HW * (0.86 + ((i * 7 + k * 13) % 10) / 45);
      s += `<line x1="${(cx - w).toFixed(1)}" y1="${y.toFixed(1)}" x2="${(cx + w).toFixed(1)}"
        y2="${y.toFixed(1)}" stroke="${col}" stroke-width="1.1"
        opacity="${(0.5 + ((i * 3 + k * 7) % 10) / 22).toFixed(2)}"/>`;
    }
  });
  /* 峰值标数 */
  const pk = counts.indexOf(peak);
  s += `<text x="${(X0 + pk * PW + PW / 2).toFixed(1)}" y="${(BASE - peak * step - 7).toFixed(1)}"
    font-size="11.5" font-weight="800" fill="#1c1c1a" text-anchor="middle">${peak}</text>`;
  /* 箱界刻度：每 3 箱标一次 IC 值 */
  s += `<line x1="${X0 - 4}" y1="${BASE + 6}" x2="${X0 + n * PW + 4}" y2="${BASE + 6}"
    stroke="#deddd6" stroke-width="1"/>`;
  for (let i = 0; i <= n; i++) {
    const x = X0 + i * PW;
    s += `<line x1="${x.toFixed(1)}" y1="${BASE + 6}" x2="${x.toFixed(1)}"
      y2="${BASE + (i % 3 === 0 ? 13 : 9)}" stroke="#cfcec7" stroke-width="0.7"/>`;
    if (i % 3 === 0 && i < n) {
      s += `<text x="${x.toFixed(1)}" y="${BASE + 24}" font-size="8" fill="#8f8e88"
        text-anchor="middle">${edges[i].toFixed(2)}</text>`;
    }
  }
  /* 中位数小旗 */
  let acc = 0, midBin = 0;
  for (let i = 0; i < n; i++) { acc += counts[i]; if (acc >= total / 2) { midBin = i; break; } }
  const mx = X0 + midBin * PW + PW * 0.72;
  s += `<line x1="${mx.toFixed(1)}" y1="${BASE + 6}" x2="${mx.toFixed(1)}"
    y2="${(BASE - counts[midBin] * step - 12).toFixed(1)}"
    stroke="#8f8e88" stroke-width="1" stroke-dasharray="2 4"/>`;
  s += `<text x="${(mx + 5).toFixed(1)}" y="${(BASE - counts[midBin] * step - 14).toFixed(1)}"
    font-size="8.5" font-weight="700" fill="#6a6963">中位数</text>`;
  /* 图注 */
  s += `<text x="${X0}" y="${H - 4}" font-size="7.5" fill="#b0afa9" letter-spacing=".1em">
    ONE RUNG = ONE OBSERVATION · ${total} 个观测 · ${n} 个 IC 区间</text>`;

  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:100%">${s}</svg>`;
}

function renderMonthly(f, box) {
  const m = f.ic_monthly_matrix;
  if (!m.years.length) return;
  const data = [];
  let lo = 0, hi = 0;
  m.years.forEach((y, yi) => m.values[yi].forEach((v, mi) => {
    if (v === null) return;
    lo = Math.min(lo, v); hi = Math.max(hi, v);
    data.push([mi, yi, v]);
  }));
  const span = Math.max(Math.abs(lo), Math.abs(hi), 0.02);
  mount(box + '_mon', {
    tooltip: { ...TOOLTIP(), trigger: 'item',
      formatter: (p) => `${m.years[p.value[1]]}-${String(p.value[0] + 1).padStart(2, '0')}<br/>IC ${p.value[2].toFixed(4)}` },
    grid: { left: 46, right: 58, top: 10, bottom: 28 },
    xAxis: { type: 'category', data: m.months.map(x => x + '月'), ...AXIS(), splitLine: { show: false } },
    yAxis: { type: 'category', data: m.years, ...AXIS(), splitLine: { show: false } },
    visualMap: { min: -span, max: span, calculable: false, orient: 'vertical', right: 0, top: 'center',
      itemWidth: 10, itemHeight: 76, precision: 3, textStyle: { fontSize: 9, color: '#9ca3af' },
      inRange: { color: [DOWN(), '#f3f4f6', UP()] } },
    series: [{ type: 'heatmap', data, label: { show: false },
      itemStyle: { borderColor: '#fff', borderWidth: 1 } }]
  });
}

function renderEquity(f, box) {
  const e = f.quantile_equity;
  if (!e.dates || !e.dates.length) return;
  const qColors = Q_COLORS();
  const series = e.series.map((s, i) => ({
    name: s.name, type: 'line', data: s.data, showSymbol: false,
    lineStyle: { width: 1.3 }, itemStyle: { color: qColors[i] }
  }));
  if (e.long_short) series.push({
    name: e.long_short.name, type: 'line', data: e.long_short.data, showSymbol: false,
    lineStyle: { width: 2 }, itemStyle: { color: UP() }, z: 5
  });
  if (e.benchmark) series.push({
    name: e.benchmark.name, type: 'line', data: e.benchmark.data, showSymbol: false,
    lineStyle: { width: 1, type: 'dashed' }, itemStyle: { color: '#9ca3af' }
  });
  mount(box + '_eq', {
    tooltip: TOOLTIP(), grid: GRID(),
    legend: { top: 0, right: 0, itemWidth: 10, itemHeight: 8, textStyle: { fontSize: 10, color: '#6b7280' } },
    xAxis: { type: 'category', data: e.dates, ...AXIS(), splitLine: { show: false } },
    yAxis: { type: 'value', scale: true, ...AXIS() },
    series
  });
}

function renderQBar(f, box) {
  const m = f.monotonicity.mean_returns || {};
  const keys = Object.keys(m).sort();
  if (!keys.length) return;
  const qColors = Q_COLORS();
  mount(box + '_qbar', {
    tooltip: { ...TOOLTIP(), trigger: 'item', formatter: (p) => `${p.name}<br/>平均收益 ${(p.value * 100).toFixed(3)}%` },
    grid: GRID(),
    xAxis: { type: 'category', data: keys, ...AXIS(), splitLine: { show: false } },
    yAxis: { type: 'value', ...AXIS(), axisLabel: { color: '#6b7280', fontSize: 10, formatter: (v) => (v * 100).toFixed(1) + '%' } },
    series: [{ type: 'bar', barWidth: '52%',
      data: keys.map((k, i) => ({ value: m[k], itemStyle: { color: qColors[i] } })) }]
  });
}

function renderDecay(f, box) {
  const d = f.ic_decay;
  if (!d.rows || !d.rows.length) return;
  mount(box + '_decay', {
    tooltip: { ...TOOLTIP(), trigger: 'axis',
      formatter: (ps) => { const r = d.rows[ps[0].dataIndex];
        return `${r.period} 日<br/>IC 均值 ${r.ic_mean === null ? '—' : r.ic_mean.toFixed(4)}<br/>ICIR ${r.icir === null ? '—' : r.icir.toFixed(3)}<br/>t ${r.t_value === null ? '—' : r.t_value.toFixed(2)}`; } },
    grid: GRID(),
    xAxis: { type: 'category', data: d.periods.map(p => p + '日'), ...AXIS(), splitLine: { show: false } },
    yAxis: { type: 'value', ...AXIS() },
    series: [{ type: 'bar', barWidth: '46%',
      data: d.rows.map(r => ({ value: r.ic_mean, itemStyle: { color: signColor(r.ic_mean) } })),
      markLine: { silent: true, symbol: 'none', lineStyle: { color: '#cbd5e1', type: 'dashed' },
        label: { show: false }, data: [{ yAxis: 0 }] } }]
  });
}

function renderTurnover(f, box) {
  const t = f.turnover;
  if (!t.dates || !t.dates.length) return;
  const qColors = Q_COLORS();
  mount(box + '_to', {
    tooltip: { ...TOOLTIP(), trigger: 'axis', valueFormatter: (v) => v === null ? '—' : (v * 100).toFixed(1) + '%' },
    grid: GRID(),
    legend: { top: 0, right: 0, itemWidth: 10, itemHeight: 8, textStyle: { fontSize: 10, color: '#6b7280' } },
    xAxis: { type: 'category', data: t.dates, ...AXIS(), splitLine: { show: false } },
    yAxis: { type: 'value', max: 1, ...AXIS(), axisLabel: { color: '#6b7280', fontSize: 10, formatter: (v) => (v * 100).toFixed(0) + '%' } },
    series: t.series.map((s, i) => ({
      name: s.name, type: 'line', data: s.data, showSymbol: false,
      lineStyle: { width: 1.1 }, itemStyle: { color: qColors[i] }
    }))
  });
}

function renderStatsTable(f, box) {
  const el = document.getElementById(box + '_st');
  if (!el) return;
  const s = f.quantile_stats || {};
  const keys = Object.keys(s).sort();
  if (!keys.length) return;
  const pct = (v) => v === null || v === undefined ? '—' : (v * 100).toFixed(2) + '%';
  const num = (v) => v === null || v === undefined ? '—' : v.toFixed(3);
  el.innerHTML = '<table class="fr-table"><thead><tr><th>分层</th><th>累计</th><th>年化</th>'
    + '<th>夏普</th><th>最大回撤</th><th>胜率</th><th>期数</th></tr></thead><tbody>'
    + keys.map(k => {
        const r = s[k];
        return `<tr><td>${k}</td><td class="${r.total_return >= 0 ? 'pos' : 'neg'}">${pct(r.total_return)}</td>`
          + `<td class="${r.annual_return >= 0 ? 'pos' : 'neg'}">${pct(r.annual_return)}</td>`
          + `<td>${num(r.sharpe)}</td><td>${pct(r.max_drawdown)}</td>`
          + `<td>${pct(r.win_rate)}</td><td>${r.n_periods}</td></tr>`;
      }).join('') + '</tbody></table>';
}

function renderFactor(fid) {
  const f = P.factors[fid];
  if (!f) return;
  const box = 'f_' + fid;
  renderIC(f, box); renderHist(f, box); renderMonthly(f, box);
  renderEquity(f, box); renderQBar(f, box); renderDecay(f, box);
  renderTurnover(f, box); renderStatsTable(f, box);
}

document.querySelectorAll('details.fr-factor').forEach((d) => {
  d.addEventListener('toggle', () => {
    if (d.open) renderFactor(d.dataset.fid);
  });
});
window.addEventListener('resize', () => Object.values(charts).forEach(c => c.resize()));
"""


def _kpi_cards(payload: dict) -> str:
    ranking = payload.get("ranking") or []
    sig = payload.get("significance") or {}
    cov = payload.get("coverage") or {}

    top_ic = next((r for r in ranking if r.get("rank_ic_mean") is not None), None)
    top_sharpe = max(
        [r for r in ranking if r.get("long_short_sharpe") is not None],
        key=lambda r: abs(r["long_short_sharpe"]), default=None)
    passed = sum(1 for r in ranking if (r.get("dsr") or 0) >= 0.95)
    mono_ok = sum(1 for r in ranking
                  if r.get("monotonicity") is not None and abs(r["monotonicity"]) > 0.9)

    def card(label, value, sub="", cls=""):
        return (f'<div class="fr-kpi"><div class="label">{_esc(label)}</div>'
                f'<div class="value {cls}">{value}</div>'
                f'{f"<div class=sub>{_esc(sub)}</div>" if sub else ""}</div>')

    return '<div class="fr-kpis">' + "".join([
        card("评估因子", f'{cov.get("evaluated", 0)}',
             f'失败 {cov.get("failed", 0)} · 标的 {cov.get("n_codes", 0)}'),
        card("IC 期数", f'{cov.get("n_dates", 0)}',
             f'截面样本 {cov.get("n_obs", 0):,}'),
        card("最高 |RankIC|", _fmt(top_ic and top_ic.get("rank_ic_mean"), 4, signed=True),
             top_ic["name"] if top_ic else "",
             "positive" if top_ic and (top_ic.get("rank_ic_mean") or 0) > 0 else "negative"),
        card("最高多空夏普", _fmt(top_sharpe and top_sharpe.get("long_short_sharpe"), 3),
             top_sharpe["name"] if top_sharpe else ""),
        card("DSR ≥ 0.95", f'{passed}',
             f'N = {sig.get("n_trials") or "—"} 次试验'),
        card("单调分层", f'{mono_ok}', '|ρ| > 0.9'),
    ]) + "</div>"


def _significance_bar(payload: dict) -> str:
    sig = payload.get("significance") or {}
    return (
        '<div class="fr-sig">'
        f'<span>{_icon("shield", 14)} 多重检验校正</span>'
        f'<span>试验次数 <b>N = {sig.get("n_trials") or "—"}</b></span>'
        f'<span>试验夏普标准差 <b>σ = {_fmt(sig.get("sr_std"), 4)}</b></span>'
        f'<span>方法 <b>{_esc(sig.get("method") or "—")}</b></span>'
        '<span>DSR ≥ 0.95 才视为「通过校正」；N 越大门槛越高</span>'
        '</div>'
    )


def _ranking_table(payload: dict) -> str:
    rows = []
    for r in payload.get("ranking") or []:
        ric = r.get("rank_ic_mean")
        cls = "pos" if (ric or 0) > 0 else ("neg" if (ric or 0) < 0 else "")
        dsr = r.get("dsr")
        pass_html = ('<span class="fr-pass yes">通过</span>' if (dsr or 0) >= 0.95
                     else '<span class="fr-pass no">未通过</span>')
        rows.append(
            f'<tr><td class="rank">{r.get("rank")}</td>'
            f'<td>{_esc(r.get("name"))}</td>'
            f'<td><span class="fr-cat">{_esc(r.get("category"))}</span></td>'
            f'<td class="{cls}">{_fmt(ric, 4, signed=True)}</td>'
            f'<td>{_fmt(r.get("ic_mean"), 4, signed=True)}</td>'
            f'<td>{_fmt(r.get("icir"), 3)}</td>'
            f'<td>{_fmt(r.get("t_value"), 2)}</td>'
            f'<td>{_fmt(r.get("ic_win_rate"), 3, pct=True)}</td>'
            f'<td>{_fmt(r.get("monotonicity"), 2, signed=True)}</td>'
            f'<td>{_fmt(r.get("long_short_annual"), 2, pct=True, signed=True)}</td>'
            f'<td>{_fmt(r.get("long_short_sharpe"), 2)}</td>'
            f'<td>{_fmt(dsr, 3)}</td>'
            f'<td>{pass_html}</td></tr>')
    return (
        '<div class="fr-card"><table class="fr-table"><thead><tr>'
        '<th>#</th><th>因子</th><th>类别</th><th>RankIC</th><th>IC</th>'
        '<th>ICIR(Rank)</th><th>t 值</th><th>RankIC&gt;0</th><th>单调性</th>'
        '<th>多空年化</th><th>多空夏普</th><th>DSR</th><th>校正</th>'
        '</tr></thead><tbody>'
        + "".join(rows) + '</tbody></table></div>'
    )


def _factor_details(payload: dict, detail_top: int) -> str:
    ranking = payload.get("ranking") or []
    selected = ranking[:detail_top] if detail_top > 0 else ranking
    blocks = []
    for r in selected:
        fid = r["factor_id"]
        kv = (f'RankIC {_fmt(r.get("rank_ic_mean"), 4, signed=True)} · '
              f'ICIR {_fmt(r.get("icir"), 2)} · '
              f'多空夏普 {_fmt(r.get("long_short_sharpe"), 2)}')
        blocks.append(
            f'<details class="fr-factor" data-fid="{_esc(fid)}">'
            f'<summary><span class="name">{_esc(r.get("name"))}</span>'
            f'<span class="id">{_esc(fid)}</span>'
            f'<span class="kv">{_esc(kv)}</span></summary>'
            f'<div class="fr-detail"><div class="fr-grid">'
            + _chart_box("IC / RankIC 时序", "activity", f'f_{fid}_ic', "")
            + _chart_box("IC 分布", "chart-column", f'f_{fid}_hist', "")
            + _chart_box("月度 IC 热力图", "calendar", f'f_{fid}_mon', "")
            + _chart_box("分层净值（Q1 最小 → Q5 最大）", "layers", f'f_{fid}_eq', "tall")
            + _chart_box("分层平均收益", "arrows", f'f_{fid}_qbar', "")
            + _chart_box("IC 衰减（持有期）", "trending-down", f'f_{fid}_decay', "")
            + _chart_box("分层换手率", "activity", f'f_{fid}_to', "")
            + f'<div class="fr-chart-box"><div class="fr-chart-title">{_icon("chart-column", 14)}'
              f'<span>分层统计</span></div><div id="f_{fid}_st"></div></div>'
            + '</div>'
            f'<p class="fr-note">分层口径：Q1 = 因子值最小，Q5 = 因子值最大；'
            f'多空 = Q5 − Q1。因子方向由 IC 符号判断，标签不预设多空。</p>'
            '</div></details>')
    omitted = len(ranking) - len(selected)
    note = (f'<p class="fr-note">仅展开前 {len(selected)} 个因子；'
            f'另有 {omitted} 个因子见上表（完整数据可通过 API 单独获取）。</p>'
            if omitted > 0 else "")
    return ('<div class="fr-card">' + "".join(blocks) + '</div>' + note)


def _chart_box(title: str, icon: str, cid: str, extra_cls: str) -> str:
    return (f'<div class="fr-chart-box"><div class="fr-chart-title">{_icon(icon, 14)}'
            f'<span>{_esc(title)}</span></div>'
            f'<div class="fr-chart {extra_cls}" id="{_esc(cid)}"></div></div>')


def _appendix(payload: dict) -> str:
    ap = payload.get("appendix") or {}
    summary = ap.get("summary") or {}
    hygiene = ap.get("hygiene") or {}
    labels = [
        ("数据区间", summary.get("data_range")),
        ("收益周期", summary.get("ret_period")),
        ("调仓频率", summary.get("rebalance")),
        ("复权方式", summary.get("adjust")),
        ("中性化处理", summary.get("neutralize")),
        ("费用假设", summary.get("cost")),
    ]
    cells = "".join(
        f'<div><div class="k">{_esc(k)}</div><div class="v">{_esc(v or "—")}</div></div>'
        for k, v in labels)
    caveats = "".join(
        f'<div class="fr-caveat">{_icon("alert", 14)}<span>{_esc(c)}</span></div>'
        for c in (ap.get("caveats") or []))
    hyg = " · ".join([
        f'停牌剔除 {"开" if hygiene.get("exclude_suspended") else "关"}',
        f'涨跌停剔除 {"开" if hygiene.get("exclude_limit") else "关"}'
        f'（{hygiene.get("limit_threshold") or "—"}）',
        f'ST {hygiene.get("st") or "—"}',
        f'次新 {hygiene.get("sub_new") or "—"}',
    ])
    return (
        '<div class="fr-card"><div class="fr-summary">' + cells + '</div>'
        f'<div class="fr-caveat">{_icon("info", 14)}<span>数据卫生：{_esc(hyg)}</span></div>'
        + caveats + '</div>'
    )


def build_report_html(payload: dict, meta: Optional[dict] = None,
                      detail_top: int = 8,
                      echarts_js: Optional[str] = None) -> str:
    """生成单文件 HTML 报告。

    Args:
        payload: ``factor_eval.evaluate_factors`` 的返回。
        meta: ``factor_data.load_panel_inputs`` 的 meta。
        detail_top: 展开详情的因子数（其余只进排名表）；<=0 = 全部展开。
        echarts_js: ECharts 源码；None = 从 vendor 目录读取。
    """
    meta = meta or {}
    js_lib = _load_echarts() if echarts_js is None else echarts_js
    sig = payload.get("significance") or {}
    generated = meta.get("generated_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = f'因子研究报告 · {meta.get("index_code", "000300")}'

    head_meta = " · ".join(filter(None, [
        f'标的池 {meta.get("index_code", "000300")}（{meta.get("loaded", 0)} 只）',
        f'区间 {meta.get("start", "—")} ~ {meta.get("end", "—")}',
        f'持有 {payload.get("params", {}).get("ret_period", "—")} 交易日',
        f'{payload.get("params", {}).get("quantiles", 5)} 分层',
        f'生成 {generated}',
    ]))

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="fr-wrap">
  <header class="fr-head">
    <div>
      <h1>{_esc(title)}</h1>
      <div class="fr-meta">{_esc(head_meta)}</div>
    </div>
    <span class="fr-badge">{_icon("shield", 14)} 口径已锁定 · 可复现</span>
  </header>

  {_kpi_cards(payload)}
  {_significance_bar(payload)}

  <section class="fr-section">
    <h2>{_icon("ranking", 16)} 因子排名（按 |RankIC| 降序）</h2>
    {_ranking_table(payload)}
    <p class="fr-note">RankIC = 日频截面 Spearman 秩相关（主口径，抗极端值）；
    IC = Pearson 对照，受涨跌停/小市值极端值影响更大，两者符号不一致属正常。
    「校正」列指 DSR ≥ 0.95——即在本次试验次数下，该因子多空夏普不太可能是运气。</p>
  </section>

  <section class="fr-section">
    <h2>{_icon("activity", 16)} 因子详情</h2>
    {_factor_details(payload, detail_top)}
  </section>

  <section class="fr-section">
    <h2>{_icon("info", 16)} 口径摘要</h2>
    {_appendix(payload)}
  </section>

  <div class="fr-foot">
    因子评估引擎 · {_esc(payload.get("params", {}).get("ic_method", "spearman"))} IC ·
    N = {sig.get("n_trials") or "—"} 次试验 ·
    生成于 {_esc(generated)}
  </div>
</div>
<script>{js_lib}</script>
<script>window.__FACTOR_PAYLOAD__ = {json.dumps(payload, ensure_ascii=False)};</script>
<script>{_JS}</script>
</body>
</html>"""
