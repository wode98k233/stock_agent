"""研究台账页渲染冒烟测试 —— Node + DOM 桩，真跑一遍渲染函数。

**存在理由**：`node --check` 只验证语法，抓不到运行时类型错误。
真实事故：`Math.abs(f.unit || '').indexOf('亿元')` —— `Math.abs` 对字符串得到 `NaN`，
再取 `.indexOf` 直接抛 `TypeError: ... .indexOf is not a function`，
页面表现是整块「读取失败」，而语法检查完全通过。

因此这里不比对文案，而是**把渲染函数真正执行一遍**，只要抛异常就失败。
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PAGE_JS = ROOT / "server" / "static" / "js" / "thesis.js"

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');

const pageJs = fs.readFileSync(process.env.THESIS_JS, 'utf8');
const fx = JSON.parse(fs.readFileSync(process.env.THESIS_FIXTURE, 'utf8'));
// render：默认，输出 thMain 的 HTML；evidence：点开单因子证据面板；report：走一遍报告图片
const MODE = process.env.THESIS_MODE || 'render';

const events = { charts: [], bodyCreated: [], bodyRemoved: [], exported: null,
                 fetches: [], localStorage: {} };
let formatterError = null;
let exportOpts = null;

// localStorage 桩：隐私模式/Node 里可能整块不存在（见 chart_version_nostore 模式，
// 那里访问 window.localStorage 直接抛异常），所以被测代码必须 try/catch 后走内存兜底。
function makeLocalStorage() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(String(k)) ? m.get(String(k)) : null),
    setItem: (k, v) => { m.set(String(k), String(v)); events.localStorage[String(k)] = String(v); },
    removeItem: (k) => { m.delete(String(k)); },
  };
}

// 活动图表数（init 记 +1、dispose 记 -1）：用来验证切台账/关页不残留旧实例
function activeCharts(prefix) {
  let n = 0;
  events.charts.forEach((c) => {
    if (String(c.id || '').indexOf(prefix) !== 0) return;
    n += c.disposed ? -1 : 1;
  });
  return n;
}

function makeEl(id, tag) {
  const el = {
    id: id || '', tagName: String(tag || 'div').toUpperCase(), innerHTML: '', textContent: '',
    style: {}, dataset: {}, disabled: false, children: [], parentNode: null, className: '',
    scrollWidth: 1240, scrollHeight: 900, offsetWidth: 1240, offsetHeight: 900, clientWidth: 1240,
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
      toggle(c, on) { if (on === undefined) on = !this._s.has(c); on ? this._s.add(c) : this._s.delete(c); },
    },
    addEventListener() {}, removeEventListener() {},
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
    removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); c.parentNode = null; return c; },
    remove() { if (this.parentNode) this.parentNode.removeChild(this); },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    getBoundingClientRect() { return { width: this.clientWidth, height: this.offsetHeight, top: 0, left: 0 }; },
    setAttribute() {}, getAttribute() { return null; },
  };
  return el;
}
const els = {};
const getEl = (id) => (els[id] || (els[id] = makeEl(id)));

const bodyStub = {
  children: [],
  appendChild(el) {
    el.parentNode = bodyStub; bodyStub.children.push(el);
    events.bodyCreated.push({
      id: el.id || '', className: el.className || '',
      html: typeof el.innerHTML === 'string' ? el.innerHTML : '',
    });
    return el;
  },
  removeChild(el) {
    const i = bodyStub.children.indexOf(el);
    if (i >= 0) bodyStub.children.splice(i, 1);
    el.parentNode = null;
    events.bodyRemoved.push(el.id || el.className || el.tagName);
    return el;
  },
};

const document = {
  getElementById: getEl,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: (t) => makeEl('', t),
  addEventListener() {},
  removeEventListener() {},
  head: { appendChild() {}, removeChild() {} },
  body: bodyStub,
};

const echartsStub = {
  init(el, theme, opts) {
    events.charts.push({ id: (el && el.id) || '', renderer: (opts && opts.renderer) || 'canvas' });
    return {
      setOption(opt) {
        // 顺手把 tooltip formatter 跑一遍：这是图表里唯一带索引取数的逻辑
        try {
          if (opt.tooltip && typeof opt.tooltip.formatter === 'function') {
            opt.tooltip.formatter([{ dataIndex: 0, value: [0, 0, 0] }]);
          }
        } catch (e) { formatterError = formatterError || e; }
        if (opt.series && !Array.isArray(opt.series)) {
          formatterError = formatterError || new Error('series 不是数组');
        }
        (opt.series || []).forEach((s, i) => {
          if (s && s.data !== undefined && typeof s.data.map !== 'function') {
            formatterError = formatterError || new Error('series[' + i + '].data 不是数组');
          }
        });
        const x = opt.xAxis;
        if (x && !Array.isArray(x) && x.type === 'category' && typeof x.data.map !== 'function') {
          formatterError = formatterError || new Error('xAxis.data 不是数组');
        }
        if (opt.xAxis && Array.isArray(opt.xAxis)) {
          opt.xAxis.forEach((ax) => {
            if (ax.type === 'category' && typeof ax.data.map !== 'function') {
              formatterError = formatterError || new Error('xAxis.data 不是数组');
            }
          });
        }
      },
      dispose() { events.charts.push({ id: (el && el.id) || '', disposed: true }); },
      resize() {},
      getDom() { return el; },
    };
  },
};

const escStub = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const windowStub = {};
windowStub.echarts = echartsStub;
windowStub.StockRadar = { utils: { esc: escStub, toast() {}, loadScriptOnce: () => Promise.resolve(echartsStub) } };
// nostore 模式：模拟隐私模式里 localStorage 一碰就抛（Safari 私有模式的行为）
if (MODE === 'chart_version_nostore') {
  Object.defineProperty(windowStub, 'localStorage', {
    configurable: true,
    get() { throw new Error('SecurityError: localStorage is not available'); },
  });
} else {
  windowStub.localStorage = makeLocalStorage();
}

global.document = document;
global.window = windowStub;
global.echarts = echartsStub;
global.requestAnimationFrame = (fn) => setTimeout(fn, 0);
global.URL = { createObjectURL: () => 'blob:stub', revokeObjectURL() {} };
// 桩 fetch：见 events.fetches 记录请求 URL（force=1 断言靠它）；
// fixture 值是数组时按顺序返回（版本变更「第一次记录、第二次提示」需要连续两次不同响应）。
const fetchCalls = {};
global.fetch = (url) => {
  const u = String(url);
  events.fetches.push(u);
  const key = u.replace(/^https?:\/\/[^/]+/, '');
  let body = fx[key];
  if (Array.isArray(body)) {
    const n = fetchCalls[key] || 0;
    fetchCalls[key] = n + 1;
    body = body[Math.min(n, body.length - 1)];
  }
  if (!body) body = fx['__default__'];
  if (!body) return Promise.reject(new Error('no fixture for ' + u));
  return Promise.resolve({ json: () => Promise.resolve(body) });
};

vm.runInThisContext(pageJs, { filename: 'thesis.js' });

const api = windowStub.StockRadar && windowStub.StockRadar.thesis;
if (!api) { console.error('NO_API'); process.exit(4); }

function finish() {
  if (formatterError) {
    console.error('CHART_ERROR: ' + formatterError.message);
    process.exit(5);
  }
  process.stdout.write(getEl('thMain').innerHTML
    // 市场矩阵是异步填充到独立容器的：桩 DOM 不会把它合并进 thMain.innerHTML，
    // 这里显式拼上，否则断言的是「还没渲染」。
    + getEl('thMatrix').innerHTML);
}

async function main() {
  const id = process.env.THESIS_ID || 'dividend_env';

  if (MODE === 'chart_version' || MODE === 'chart_version_nostore'
      || MODE === 'chart_version_seeded') {
    // 连续「进详情页」3 次：第一次只记录、第二次起按 def_hash 变化提示。
    // seeded 模式先在 localStorage 里放一条旧记录，模拟「上次访问后定义被改过」。
    if (MODE === 'chart_version_seeded') {
      try {
        windowStub.localStorage.setItem('sr_thesis_chart_ver_v1', JSON.stringify({
          dividend_env: { def_hash: 'stale-hash', def_version: '1.0' },
        }));
      } catch (e) { /* 忽略：nostore 场景才需要 */ }
    }
    const rounds = [];
    for (let r = 0; r < 3; r++) {
      await api.loadDetail(id);
      await new Promise((resolve) => setTimeout(resolve, 150));
      rounds.push({
        notice: getEl('thScoreChartNotice').textContent,
        display: getEl('thScoreChartNotice').style.display,
        sub: getEl('thScoreChartSub').innerHTML,
      });
    }
    let store = null;
    try {
      store = JSON.parse(windowStub.localStorage.getItem('sr_thesis_chart_ver_v1') || 'null');
    } catch (e) { store = null; }   // nostore：访问即抛，兜底为 null
    process.stdout.write(JSON.stringify({
      rounds, store,
      writes: events.localStorage['sr_thesis_chart_ver_v1'] || null,
    }));
    return;
  }

  await api.loadDetail(id);
  // 组合建议与市场矩阵是 renderDetail 里 fire-and-forget 的异步块，
  // 必须让出事件循环等它们落地，否则断言的是「还没渲染」的 DOM。
  await new Promise((resolve) => setTimeout(resolve, 120));

  if (MODE === 'evidence') {
    const key = process.env.THESIS_FACTOR || 'premium';
    api.toggleEvidence(id, key, null);
    await new Promise((resolve) => setTimeout(resolve, 250));
    if (formatterError) {
      console.error('CHART_ERROR: ' + formatterError.message);
      process.exit(5);
    }
    // 证据详情已改为统一停靠区（#thEvDockBody），不再塞回单张因子卡
    process.stdout.write(getEl('thEvDockBody').innerHTML);
    return;
  }

  if (MODE === 'evidence_tabs') {
    // 同时打开两个（且顺序上不相邻）的因子：只应新增标签，不应重排卡片
    api.toggleEvidence(id, 'premium', null);
    await new Promise((resolve) => setTimeout(resolve, 250));
    api.toggleEvidence(id, 'style_rel', null);
    await new Promise((resolve) => setTimeout(resolve, 250));
    process.stdout.write(JSON.stringify({
      tabs: getEl('thEvTabs').innerHTML,
      count: getEl('thEvDockCount').textContent,
      body: getEl('thEvDockBody').innerHTML,
      dockDisplay: getEl('thEvDock').style.display,
    }));
    return;
  }

  if (MODE === 'report') {
    // 报告图片：chat-export 用桩，只验证「离屏容器 + SVG renderer + 清理 + 预览」
    windowStub.StockRadar.chatExport = {
      elementToPngBlob(el, opts) {
        exportOpts = opts || {};
        events.exported = { id: el && el.id, width: el && el.scrollWidth };
        return Promise.resolve({ size: 1, stub: true });
      },
      downloadBlob() {},
    };
    await api.generateReport();
    await new Promise((resolve) => setTimeout(resolve, 120));
    if (formatterError) {
      console.error('CHART_ERROR: ' + formatterError.message);
      process.exit(5);
    }
    const svg = events.charts.filter((c) => c.renderer === 'svg').map((c) => c.id);
    const modal = bodyStub.children.filter((c) => c.className === 'th-report-modal')[0] || null;
    const reportNode = events.bodyCreated.filter((c) => c.id === 'thReportRoot')[0] || null;
    process.stdout.write(JSON.stringify({
      svgCharts: svg,
      reportRemoved: events.bodyRemoved.indexOf('thReportRoot') >= 0,
      reportHtml: reportNode ? reportNode.html : '',
      preview: !!modal,
      previewHtml: modal ? modal.innerHTML.slice(0, 4000) : '',
      pixelRatio: exportOpts && exportOpts.pixelRatio,
      exportWidth: events.exported && events.exported.width,
      disposed: events.charts.filter((c) => c.disposed).length,
      // 报告并图：导出前必须已经 init 过 #thReportScore；失败时该节隐藏、note 写原因
      scoreInit: events.charts.some((c) => c.id === 'thReportScore' && !c.disposed),
      scoreDisposed: events.charts.filter(
        (c) => c.id === 'thReportScore' && c.disposed).length,
      scoreDisplay: getEl('thReportScore').style.display,
      scoreNote: getEl('thReportScoreNote').textContent,
      scoreNoteDisplay: getEl('thReportScoreNote').style.display,
      fetches: events.fetches,
    }));
    return;
  }

  if (MODE === 'chart_overview') {
    // 「◫ 双轴总览」：面板打开 → 并发拉各台账小图（桩里只有 2 张数据）
    await api.openScorePanel();
    await new Promise((resolve) => setTimeout(resolve, 80));
    if (formatterError) {
      console.error('CHART_ERROR: ' + formatterError.message);
      process.exit(5);
    }
    const notes = [0, 1].map((i) => ({
      text: getEl('thChartNote_' + i).textContent,
      display: getEl('thChartNote_' + i).style.display,
    }));
    // 卡片头部是「innerHTML 拼骨架 + 取到数据后回填」：桩 DOM 不解析 innerHTML，
    // 所以版本号/ρ 只能从元素本身读（真实浏览器里它们就长在卡片头上）。
    const vers = [0, 1].map((i) => getEl('thChartVer_' + i).textContent);
    const rhos = [0, 1].map((i) => getEl('thChartRho_' + i).innerHTML);
    process.stdout.write(JSON.stringify({
      panel: getEl('thChartPanel').style.display,
      meta: getEl('thChartMeta').textContent,
      grid: getEl('thChartGrid').innerHTML,
      notes,
      vers,
      rhos,
      // 缓存新鲜度与版本变更圆点（卡片头上回填的两块）
      fresh: [0, 1].map((i) => getEl('thChartFresh_' + i).textContent),
      dots: [0, 1].map((i) => getEl('thChartDot_' + i).style.display),
      fetches: events.fetches,
      charts: events.charts.filter(
        (c) => String(c.id).indexOf('thChartMini_') === 0 && !c.disposed),
    }));
    return;
  }

  if (MODE === 'chart_detail') {
    // 详情页大图：loadDetail 后 _renderScoreDetail 已跑（main 里等过 120ms）
    process.stdout.write(JSON.stringify({
      sub: getEl('thScoreChartSub').innerHTML,
      note: getEl('thScoreChartNote').textContent,
      noteDisplay: getEl('thScoreChartNote').style.display,
      chartShown: activeCharts('thScoreChart') > 0,
      notice: getEl('thScoreChartNotice').textContent,
      noticeDisplay: getEl('thScoreChartNotice').style.display,
    }));
    return;
  }

  if (MODE === 'chart_force') {
    // 「↻ 重新拉取」：必须带 force=1 跳过后端 1h 缓存
    await api.openScorePanel(true);
    await new Promise((resolve) => setTimeout(resolve, 80));
    process.stdout.write(JSON.stringify({
      fetches: events.fetches,
      meta: getEl('thChartMeta').textContent,
    }));
    return;
  }

  if (MODE === 'chart_lifecycle') {
    // 关闭总览：小图实例必须被 dispose、grid 清空，隐藏容器里不留图
    await api.openScorePanel();
    await new Promise((resolve) => setTimeout(resolve, 80));
    const before = activeCharts('thChartMini_');
    api.closeScorePanel();
    await new Promise((resolve) => setTimeout(resolve, 30));
    process.stdout.write(JSON.stringify({
      before,
      after: activeCharts('thChartMini_'),
      disposed: events.charts.filter(
        (c) => String(c.id).indexOf('thChartMini_') === 0 && c.disposed).length,
      grid: getEl('thChartGrid').innerHTML,
      panel: getEl('thChartPanel').style.display,
    }));
    return;
  }

  if (MODE === 'chart_switch') {
    // 切换台账 + 关页：详情大图只保留最新一个实例，关页后归零
    const other = process.env.THESIS_ID2 || 'gold_regime';
    await api.loadDetail(other);
    await new Promise((resolve) => setTimeout(resolve, 120));
    const activeAfterSwitch = activeCharts('thScoreChart');
    api.closePage();
    await new Promise((resolve) => setTimeout(resolve, 30));
    process.stdout.write(JSON.stringify({
      activeAfterSwitch,
      afterClose: activeCharts('thScoreChart'),
      disposed: events.charts.filter((c) => c.id === 'thScoreChart' && c.disposed).length,
    }));
    return;
  }

  finish();
}

main().catch((e) => {
  console.error('RENDER_ERROR: ' + (e && e.message));
  process.exit(3);
});
"""


def _node_bin() -> str | None:
    for cand in (os.environ.get("NODE_BIN"), shutil.which("node")):
        if cand and Path(cand).exists():
            return cand
    managed = (Path.home() / ".workbuddy" / "binaries" / "node")
    if managed.is_dir():
        for exe in sorted(managed.glob("versions/*/node.exe"), reverse=True):
            return str(exe)
    return None


def _factor(key, name, value, unit, score, status, asof="2026-09-17",
            stale=False, note="", manual=False, pct_rank=None, confidence=None,
            pct_rank_off=False):
    return {"key": key, "name": name, "value": value, "unit": unit, "score": score,
            "status": status, "detail": "明细 %s" % name, "source": "测试来源",
            "asof": asof, "stale": stale, "note": note, "manual": manual,
            "pct_rank": pct_rank, "confidence": confidence,
            "pct_rank_off": pct_rank_off}


def _detail_payload():
    return {
        "code": 0, "msg": "ok",
        "data": {
            "thesis_id": "dividend_env",
            "name": "红利环境因子包",
            "asset_label": "中证红利低波动",
            "definition": {"id": "dividend_env", "version": "1.0"},
            "definition_ok": True,
            "definition_problems": [],
            "definition_history": [{"content_hash": "h1", "name": "红利环境因子包",
                                    "version": "1.0", "created_at": "2026-09-17 21:30:00",
                                    "definition": {}}],
            "snapshot": {
                "thesis_id": "dividend_env", "trade_date": "2026-09-17",
                "data_asof": "2026-09-17", "total": 4, "valid_count": 5, "factor_count": 5,
                "verdict": "中性偏有利", "action": "可持有，回调分批介入",
                "factors": [
                    _factor("premium", "① 股息率溢价", 2.59, "pp", 0, "中性"),
                    _factor("cn10_y", "② 10Y 国债", 1.6862, "%", 1, "有利",
                            pct_rank=0.4,
                            confidence={"level": "high", "why": "中债官方口径，日频"}),
                    _factor("insurance_gap", "③ 险资 / OCI 流入", 15000, "亿元", 1, "有利",
                            stale=True, note="华泰测算欠配 1.1–1.9 万亿", manual=True),
                    _factor("style_rel", "④ 成长风格相对强弱", 17.65, "pp", 2, "很有利"),
                    _factor("crowding", "⑤ 拥挤度", -1.86, "%偏离", 0, "中性"),
                    _factor("missing", "⑥ 缺数据因子", None, "", None, "数据缺失"),
                ],
                "signals": {
                    "buy": [{"label": "股息率溢价 > 3.0pp", "current": "+2.59pp", "hit": False},
                            {"label": "险资欠配 > 1 万亿元（制度性买盘仍在）",
                             "current": "15000 亿元", "hit": True}],
                    "warn": [{"label": "险资数据超过 120 天未更新", "current": "已陈旧",
                              "hit": True}],
                },
                "invalidation": [
                    {"text": "股息率溢价跌破 1.5pp：相对债券的补偿不足",
                     "current": "+2.59pp", "hit": False},
                    {"text": "10Y 国债升破 2.10%：无风险利率锚上移",
                     "current": "2.2000%", "hit": True},
                ],
                "scenarios": [
                    {"name": "基准：区间震荡", "probability": 50, "hit": True,
                     "outcome": "年度 +6% ~ +8%（含股息）", "factors": "溢价中性 / 利率有利",
                     "conditions": [{"metric": "cn10_y", "op": ">", "value": 1.6,
                                     "current": "1.6862%", "hit": True}]},
                    {"name": "谨慎：估值回归", "probability": 25, "hit": False,
                     "outcome": "年度 −5% ~ +2%", "factors": "利率上行",
                     "conditions": [{"metric": "cn10_y", "op": ">=", "value": 1.9,
                                     "current": "1.6862%", "hit": False}]},
                ],
                "phases": [
                    {"name": "极致低估 + 资金涌入期", "from": "2022-01", "to": "2025-12",
                     "feature": "溢价 +3.05 → +3.40pp", "current": False},
                    {"name": "估值修复兑现 / 分歧加大期", "from": "2026-01", "to": "",
                     "feature": "溢价压缩至 2.4~2.6pp", "current": True},
                ],
                "transmission": [
                    {"path": "① 跨境配置", "status": "基本失效", "hit": True,
                     "mechanism": "中美利差倒挂 → 套保成本上升", "effect": "中债上行概率仅 47%~54%",
                     "conditions": [{"metric": "double_tighten", "op": "<=", "value": 0,
                                     "current": "0", "hit": True}]},
                ],
                "warnings": ["险资数据更新于 2025-01-01，已超 120 天"],
            },
            "alerts": [
                {"id": 1, "thesis_id": "dividend_env", "trade_date": "2026-09-17",
                 "alert_type": "invalidation_hit", "severity": "critical",
                 "message": "结论失效条件命中：10Y 国债升破 2.10%", "detail": {},
                 "created_at": "2026-09-17 21:31:00"},
                {"id": 2, "thesis_id": "dividend_env", "trade_date": "2026-09-17",
                 "alert_type": "verdict_change", "severity": "warn",
                 "message": "综合结论由「偏有利」变为「中性偏有利」", "detail": {},
                 "created_at": "2026-09-17 21:31:00"},
                {"id": 3, "thesis_id": "dividend_env", "trade_date": "2026-09-17",
                 "alert_type": "factor_flip", "severity": "info",
                 "message": "① 股息率溢价 评分 +1 → +0（中性）", "detail": {},
                 "created_at": "2026-09-17 21:31:00"},
            ],
            "alert_summary": {"info": 1, "warn": 1, "critical": 1, "total": 3},
        },
    }


def _history_payload(n=3):
    items = []
    for i in range(n):
        items.append({"trade_date": "2026-09-1%d" % (5 + i), "total": i - 1,
                      "valid_count": 5, "factor_count": 5, "verdict": "中性",
                      "scores": {"premium": 0}})
    return {"code": 0, "msg": "ok", "data": {"thesis_id": "dividend_env",
                                             "items": items, "count": n}}


def _history_with_values(n=24):
    """带逐因子 values 的历史快照：sparkline / 历史区间 / 变化 chip / 变动归因都吃它。"""
    items = []
    for i in range(n):
        last = (i == n - 1)
        items.append({
            "trade_date": "2026-09-%02d" % (1 + i),
            "data_asof": "2026-09-%02d" % (1 + i),
            "total": (i % 3) - 1,
            "valid_count": 5, "factor_count": 6, "verdict": "中性",
            "scores": {
                "premium": 1 if last else 0,
                "cn10_y": 0 if last else 1,
                "insurance_gap": 1,
                "style_rel": 2,
                "crowding": 0,
            },
            "values": {
                "premium": round(2.10 + i * 0.02, 4),
                "cn10_y": round(1.70 + i * 0.001, 4),
                "insurance_gap": 15000,
                "style_rel": round(17.0 + i * 0.1, 2),
                "crowding": round(-1.8 + i * 0.01, 3),
            },
            "pct_ranks": {"premium": None, "cn10_y": 0.3},
            "units": {"premium": "pp", "cn10_y": "%", "insurance_gap": "亿元",
                      "style_rel": "pp", "crowding": "%偏离"},
            "statuses": {"premium": "中性", "cn10_y": "有利"},
        })
    return {"code": 0, "msg": "ok",
            "data": {"thesis_id": "dividend_env", "items": items, "count": n}}


def _evidence_payload():
    """新版证据契约（5.2）：4 张图 + 当前分位 + 方向校验 + caveats。"""
    dates = ["2022-07-%02d" % (i + 1) for i in range(30)]
    corr_value = [0.31, 0.25, -0.10, 0.20] * 7 + [0.10, 0.05]
    layers = []
    for i in range(5):
        layers.append({"bucket": i + 1, "label": "Q%d（0.8 ~ 1.5pp）" % (i + 1),
                       "lo": 0.8, "hi": 1.5, "mean_return": round(-0.4 + i * 0.5, 2),
                       "median_return": -0.3, "win_rate": 44.0 + i, "n": 42,
                       "current": i == 3})
    return {"code": 0, "msg": "ok", "data": {
        "supported": True, "factor_key": "premium", "factor_name": "① 股息率溢价",
        "unit": "pp", "discrete": False, "source": "replay",
        "target": {"code": "H30269", "name": "中证红利低波动", "alias": "red",
                   "kind": "frame", "freq": "daily", "freq_label": "日频", "n": 521},
        "sample": {"start": "2022-01-04", "end": "2026-10-27", "n": 300,
                   "effective_n": 220, "effective_start": "2024-10-08",
                   "effective_end": "2026-10-27",
                   "note": "n=因子观测总数；effective_n=有未来收益、真正进入统计的样本数"},
        "current": {"value": 2.5542, "asof": "2026-10-27", "percentile": 0.78,
                    "min": 1.2, "p25": 2.0, "median": 2.4, "p75": 2.9, "max": 3.8,
                    "change": 0.18, "change_label": "较 20 期前 +0.18pp",
                    "bucket_index": 3, "bucket_label": "Q4（2.1 ~ 3.0pp）"},
        "horizons": [{"h": 20, "label": "未来 20 个交易日", "n": 210,
                      "corr_value": 0.21, "corr_score": 0.18,
                      "spearman_value": 0.19, "spearman_score": 0.16,
                      "q5_q1": 1.3, "layers": layers}],
        "rolling": {"window": 120, "h": 20, "label": "滚动 120 期 · 未来 20 日",
                    "dates": dates, "corr_value": corr_value,
                    "corr_score": [v / 2 for v in corr_value],
                    "positive_share": 62.0, "negative_share": 38.0,
                    "current_corr_value": -0.18, "current_corr_score": -0.12,
                    "phases": [{"start": dates[0], "end": dates[-1], "sign": "positive",
                                "label": "正相关", "mean_corr": 0.28, "n": 110}]},
        "scatter": [[2.55, 1.2], [2.1, 0.4], [2.4, 0.9]],
        "scatter_score": [[1, 1.2], [0, 0.4], [1, 0.9]],
        "dual": {"dates": dates, "factor": [2.1] * 30, "target": [100.0] * 30,
                 "factor_norm": [100.0] * 30,
                 "target_norm": [100.0 + i for i in range(30)],
                 # 2026-10-27 契约：dual 改为 z-score 标准化（100 ± 10×标准差），
                 # 面板标题必须用 label，不能再写「归一化到 100」。
                 "method": "zscore_x10_base100",
                 "label": "标准化（100 ± 10×标准差，看形状不看绝对刻度）"},
        "direction": {"expected_sign": "positive", "aligned": True,
                      "score_sign": "positive",
                      "text": "读数越高，未来收益越高（r=+0.21）；评分表同样给高读数正分，方向一致。"},
        "verdict": "数据支撑较强：因子读数与未来 20 日收益呈正相关（r=+0.21，n=210）。",
        "caveats": ["相关不等于因果：只说明历史上同向。", "未来收益窗口重叠会让有效样本打折。"],
        "reason": None,
    }}


def _evidence_unsupported_payload():
    return {"code": 0, "msg": "ok", "data": {
        "supported": False, "factor_key": "insurance_gap", "factor_name": "③ 险资 / OCI 流入",
        "reason": "手工维护因子，暂无自动时序；快照样本 10 期 < 30",
        "source": "snapshot", "sample": {"n": 10},
    }}


def _evidence_short_payload():
    """常规 20/60 日期限样本不足、只用短窗口（未来 5 个交易日）出证据。

    2026-10-27 契约：horizons 末尾可能多一条 `short_window: true`，
    面板必须显式标「短窗口参考」，且标题写真实的「未来 5 个交易日」。
    """
    d = json.loads(json.dumps(_evidence_payload()))["data"]
    d["horizons"] = [
        {"h": 20, "label": "未来 20 个交易日", "n": 26, "corr_value": None,
         "corr_score": None, "q5_q1": None, "layers": []},
        {"h": 60, "label": "未来 60 个交易日", "n": 24, "corr_value": None,
         "corr_score": None, "q5_q1": None, "layers": []},
        {"h": 5, "label": "未来 5 个交易日", "short_window": True, "n": 46,
         "corr_value": 0.63, "corr_score": 0.55, "q5_q1": 2.1,
         "layers": [
             {"bucket": i + 1, "code": "Q%d" % (i + 1), "label": "Q%d" % (i + 1),
              "lo": None, "hi": None, "mean_return": round(-0.5 + i * 0.6, 2),
              "median_return": -0.1, "win_rate": 45.0 + i, "n": 9,
              "current": i == 3}
             for i in range(5)
         ]},
    ]
    d["current"]["bucket_index"] = 4
    d["current"]["bucket_label"] = "Q4"
    d["current"]["bucket_code"] = "Q4"
    return {"code": 0, "msg": "ok", "data": d}


def _report_payload():
    """报告契约（5.4）：一次组装结论 + 归因 + 历史 + 证据批量 + 告警。"""
    detail = _detail_payload()["data"]
    hist = _history_with_values(6)["data"]
    dates = ["2026-09-01", "2026-09-08", "2026-09-15"]
    return {"code": 0, "msg": "ok", "data": {
        "generated_at": "2026-10-27 20:30",
        "thesis": {"id": "dividend_env", "name": "红利环境因子包",
                   "asset_label": "中证红利低波",
                   "description": "红利环境五因子", "caliber_note": "全收益口径，含股息再投资"},
        "snapshot": detail["snapshot"],
        "history": hist,
        "definition": {"factors": [
            {"key": "premium", "name": "① 股息率溢价", "unit": "pp",
             "confidence": {"level": "high"}},
            {"key": "cn10_y", "name": "② 10Y 国债", "unit": "%",
             "confidence": {"level": "mid"}},
        ]},
        "attribution": {
            "prev_trade_date": "2026-09-22", "cur_trade_date": "2026-09-23",
            "total_delta": 1,
            "rows": [
                {"key": "premium", "name": "① 股息率溢价", "prev_score": 0,
                 "cur_score": 1, "delta": 1, "prev_value": 2.5, "cur_value": 2.55,
                 "value_delta": 0.05, "unit": "pp", "direction": "up"},
                {"key": "cn10_y", "name": "② 10Y 国债", "prev_score": 1,
                 "cur_score": 0, "delta": -1, "prev_value": 1.70, "cur_value": 1.72,
                 "value_delta": 0.02, "unit": "%", "direction": "down"},
            ],
            "note": "总分 0 → +1（+1）：① 股息率溢价 0 → +1（+1）、② 10Y 国债 +1 → 0（-1）；其余不变",
        },
        "evidence": {"ok": True, "target": {"code": "H30269", "name": "中证红利低波动"},
                     "rows": [
                         {"factor_key": "premium", "factor_name": "① 股息率溢价",
                          "tier": "strong", "supported": True, "reason": None,
                          "h": 20, "corr": 0.21, "corr_score": 0.18, "q5_q1": 1.3,
                          "n": 210, "verdict": "支撑较强", "confidence": "mid",
                          "current_value": 2.5542, "percentile": 0.78,
                          "current_bucket": "Q4", "bucket_mean_return": 0.8,
                          "bucket_win_rate": 56.0, "rolling_current": -0.18,
                          "rolling_positive_share": 62.0, "direction": "positive",
                          "aligned": True,
                          "rolling": {"dates": dates, "corr_value": [0.31, -0.2, 0.1]}},
                         {"factor_key": "insurance_gap", "factor_name": "③ 险资 / OCI 流入",
                          "tier": "unsupported", "supported": False,
                          "reason": "手工维护因子，暂无自动时序",
                          "direction": None, "aligned": None,
                          "corr": None, "corr_score": None, "q5_q1": None, "n": None},
                         # 有 direction 但没算出 corr/n：方向校验必须显示「样本不足」，
                         # 不能把「按阈值表声明」当成「已验证方向」。
                         {"factor_key": "m1m2_gap", "factor_name": "⑥ M1-M2 剪刀差",
                          "tier": "insufficient", "supported": True, "reason": None,
                          "h": 20, "corr": None, "corr_score": None, "q5_q1": None,
                          "n": None, "direction": "positive", "aligned": True,
                          "current_value": -4.0, "percentile": 0.4,
                          "current_bucket": "Q2", "bucket_mean_return": None,
                          "bucket_win_rate": None,
                          # 2008–2010 的老序列：必须落在「近 3 年」窗口外被跳过并注明
                          "rolling": {"dates": ["2010-01-04", "2010-02-01"],
                                      "corr_value": [0.2, 0.1]}},
                         # 短窗口行（h=5）：报告表必须挂「短窗口」角标 + 脚注
                         {"factor_key": "style_rel", "factor_name": "④ 成长风格相对强弱",
                          "tier": "weak", "supported": True, "reason": None,
                          "h": 5, "short_window": True, "corr": 0.629,
                          "corr_score": 0.55, "q5_q1": None, "n": 46,
                          "direction": "positive", "aligned": True,
                          "current_value": 17.14, "percentile": 0.97,
                          "current_bucket": "Q5", "bucket_mean_return": 0.9,
                          "bucket_win_rate": 58.0,
                          "rolling": {"dates": dates, "corr_value": [0.2, 0.3, 0.1]}},
                     ],
                     "summary": {"strong": 1, "unsupported": 1}, "ok": True},
        "alerts": [{"id": 1, "trade_date": "2026-09-23", "severity": "warn",
                    "message": "综合结论由「偏有利」变为「中性偏有利」"}],
        "caveats": ["相关不等于因果：只说明历史上同向。", "未来收益窗口重叠会让有效样本打折。"],
    }}


def _run(tmp_path, fixture: dict, thesis_id: str = "dividend_env",
         mode: str = "render", factor: str = None) -> str:
    node = _node_bin()
    if not node:
        pytest.skip("未找到 node，跳过前端渲染冒烟测试")

    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    fx = tmp_path / "fixture.json"
    fx.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")

    env = dict(os.environ, THESIS_JS=str(PAGE_JS), THESIS_FIXTURE=str(fx),
               THESIS_ID=thesis_id, THESIS_MODE=mode)
    if factor:
        env["THESIS_FACTOR"] = factor
    proc = subprocess.run([node, str(harness)], capture_output=True, env=env,
                          timeout=120)
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 0, "渲染失败:\n%s\n%s" % (err, out[:600])
    assert out, "渲染结果为空"
    return out


def _render(tmp_path, fixture: dict, thesis_id: str = "dividend_env") -> str:
    return _run(tmp_path, fixture, thesis_id, "render")


def _gold_detail_payload():
    """黄金台账详情：snapshot 带 `stages`（驱动力 × 位置 九宫格）。

    与 `_detail_payload` 的关键差异是「位置维度」：
    ① 读数本身就是分位（`pct_rank_off=True`），不该再画一条「历史分位」带；
    ② stages 缺席时整块必须隐藏（红利/恒科/债基三个老台账不受影响）。
    """
    d = _detail_payload()
    d["data"]["thesis_id"] = "gold_regime"
    d["data"]["name"] = "黄金阶段研判台账"
    d["data"]["asset_label"] = "黄金（上海金 Au99.99；国际金价为合成口径）"
    snap = d["data"]["snapshot"]
    snap["thesis_id"] = "gold_regime"
    snap["total"] = 1
    snap["factor_count"] = 7
    snap["valid_count"] = 7
    snap["verdict"] = "中性"
    snap["action"] = "观望，等美元或短端利率给出方向"
    snap["factors"] = [
        _factor("dxy", "① 美元指数（合成）", 142.3, "bp", -1, "偏不利"),
        _factor("us2_y", "② 美债 2Y", 68.0, "bp", -2, "不利"),
        _factor("real", "③ 实际利率（代理）", 0.0, "pp", 0, "中性"),
        _factor("gold_etf", "④ 黄金 ETF 库存", 1.72, "%", 1, "有利"),
        _factor("agratio", "⑤ 金银比", -7.12, "%", 2, "很有利"),
        _factor("cn_gold", "⑥ 中国黄金储备", 2.36, "%", 1, "有利"),
        _factor("position", "⑦ 价格位置（延伸度分位）", 55.0, "%分位", 0, "中性",
                pct_rank_off=True),
    ]
    snap["signals"] = {"buy": [{"label": "金银比近 20 日回落超 1%",
                                "current": "-7.12%", "hit": True}],
                       "warn": [{"label": "美债 2Y 近 20 日上行超 10bp",
                                 "current": "+68.0bp", "hit": True}]}
    snap["scenarios"] = []
    snap["transmission"] = []
    snap["phases"] = []
    snap["stages"] = {
        "drive_keys": ["dxy", "us2_y", "real", "gold_etf", "agratio", "cn_gold"],
        "drive_total": 1, "drive_valid_count": 6, "drive_factor_count": 6,
        "drive_missing": [], "drive_band": "中性", "drive_full": 11,
        "pos_key": "position", "pos_score": 0, "pos_band": "中性位",
        "pos_full": 2, "complete": True,
        "stage": "中性震荡期", "desc": "驱动与位置都居中，无明确边际", "tone": "neutral",
        "drive_bands": [{"name": "顺风", "min": 4}, {"name": "中性", "min": -3},
                        {"name": "逆风", "min": -99}],
        "pos_bands": [{"name": "低位", "min": 1}, {"name": "中性位", "min": -1},
                      {"name": "高位", "min": -99}],
        "cells": [
            {"drive": "顺风", "pos": "低位", "stage": "低位积累期", "tone": "bull",
             "desc": "环境有利 + 位置便宜", "current": False},
            {"drive": "顺风", "pos": "中性位", "stage": "顺风上行期", "tone": "bull",
             "desc": "驱动有利、位置居中", "current": False},
            {"drive": "顺风", "pos": "高位", "stage": "趋势延伸期", "tone": "warn",
             "desc": "持有可以，追高不宜", "current": False},
            {"drive": "中性", "pos": "低位", "stage": "低位磨底期", "tone": "warn",
             "desc": "等驱动拐点", "current": False},
            {"drive": "中性", "pos": "中性位", "stage": "中性震荡期", "tone": "neutral",
             "desc": "驱动与位置都居中，无明确边际", "current": True},
            {"drive": "中性", "pos": "高位", "stage": "高位僵持期", "tone": "neutral",
             "desc": "位置已高但缺乏驱动", "current": False},
            {"drive": "逆风", "pos": "低位", "stage": "逆风错杀期", "tone": "neutral",
             "desc": "可能是布局窗口", "current": False},
            {"drive": "逆风", "pos": "中性位", "stage": "逆风调整期", "tone": "bear",
             "desc": "承压", "current": False},
            {"drive": "逆风", "pos": "高位", "stage": "高位风险期", "tone": "bear",
             "desc": "风险最大的组合", "current": False},
        ],
    }
    return d


@pytest.fixture
def fixture_gold():
    return {"/api/thesis/gold_regime": _gold_detail_payload(),
            "/api/thesis/market-matrix": _matrix_payload(),
            "/api/thesis/portfolio": {"code": 0, "msg": "ok", "data": {"ok": False,
                                                                     "reason": "未配置"}},
            "/api/thesis/list": {"code": 0, "data": {"items": [], "count": 0}},
            "__default__": {"code": 0, "data": {"thesis_id": "gold_regime",
                                                "items": [], "count": 0}}}


def _matrix_payload():
    return {"code": 0, "msg": "ok", "data": {
        "asof": "2026-09-22", "windows": ["1日", "5日"],
        "rows": [{"code": "H30269", "name": "红利低波", "last": 10897.72,
                  "values": [0.03, -1.12]},
                 {"code": "HSTECH", "name": "恒生科技", "last": 4423.29,
                  "values": [0.40, 2.44]}],
        "correlation": {"labels": ["红利低波", "恒生科技"],
                        "matrix": [[1.0, 0.07], [0.07, 1.0]],
                        "basis": "近 250 个共同交易日的日收益（Pearson）"},
        "missing": [],
    }}


def _list_payload(items):
    return {"code": 0, "msg": "ok", "data": {"items": items, "count": len(items)}}


def _list_item(tid, name, label="测试标的"):
    return {"thesis_id": tid, "name": name, "asset_label": label, "has_snapshot": True,
            "verdict": "中性", "total": 1, "trade_date": "2026-09-17"}


def _chart_payload(thesis_id="dividend_env", name="红利环境因子包", version="1.0",
                   n=6, h=3, def_hash="h1", cached=True, cache_age_s=600,
                   computed_at=None):
    """双轴图契约：dates/total/flip/price/fut + metrics + 缓存元数据。

    `cached` / `cache_age_s` / `computed_at` 是后端 1h 缓存的新字段；
    `def_hash`/`def_version` 驱动「定义已更新」提示（同一 fixture 可给数组模拟连续变化）。
    """
    dates = ["2026-09-%02d" % (1 + i) for i in range(n)]
    total = [(i % 5) - 2 for i in range(n)]
    flip = [-v for v in total]
    price = [round(1000 + i * 3.5, 2) for i in range(n)]
    fut = [price[i + h] if i + h < n else None for i in range(n)]
    data = {
        "thesis_id": thesis_id, "name": name, "def_version": version,
        "def_hash": def_hash,
        "target": {"code": "H30269", "name": "中证红利低波动", "freq": "daily",
                   "freq_label": "日频"},
        "h": h, "n": n, "range": [dates[0], dates[-1]],
        "generated_at": "2026-11-01 22:00:00", "cached": cached,
        "metrics": {"r_level": 0.153, "r_diff": -0.02, "r_fwd": 0.15,
                    "r_fwd_flip": 0.231},
        "signs": {}, "dropped": [], "points": n, "thin": False,
        "dates": dates, "total": total, "flip": flip, "price": price, "fut": fut,
    }
    if cache_age_s is not None:
        data["cache_age_s"] = cache_age_s
    if computed_at:
        data["computed_at"] = computed_at
    return {"code": 0, "msg": "ok", "data": data}


@pytest.fixture
def fixture_single_point():
    return {"/api/thesis/dividend_env": _detail_payload(),
            "/api/thesis/market-matrix": _matrix_payload(),
            "/api/thesis/portfolio": {"code": 0, "msg": "ok", "data": {"ok": False,
                                                                     "reason": "未配置"}},
            "/api/thesis/list": {"code": 0, "data": {"items": [], "count": 0}},
            "__default__": {"code": 0, "data": {"thesis_id": "dividend_env",
                                                "items": [], "count": 0}}}


@pytest.fixture
def fixture_with_chart():
    # __default__ 兜住 history 与 list，避免逐个 URL 造桩
    return {"/api/thesis/dividend_env": _detail_payload(),
            "__default__": _history_payload(3)}


def _base_fixture(extra: dict) -> dict:
    """详情 + 历史 + 矩阵 + 组合的公共桩，用例只补自己关心的 URL。"""
    fx = {"/api/thesis/dividend_env": _detail_payload(),
          "/api/thesis/market-matrix": _matrix_payload(),
          "/api/thesis/portfolio": {"code": 0, "msg": "ok",
                                    "data": {"ok": False, "reason": "未配置"}},
          "/api/thesis/list": {"code": 0, "data": {"items": [], "count": 0}},
          "/api/thesis/dividend_env/history?days=180": _history_with_values(24),
          "/api/thesis/dividend_env/report": _report_payload(),
          "/api/thesis/dividend_env/evidence": _evidence_payload(),
          "__default__": {"code": 0, "data": {"thesis_id": "dividend_env",
                                              "items": [], "count": 0}}}
    fx.update(extra)
    return fx


@pytest.fixture
def fixture_rich():
    """带 values 的历史：sparkline / 历史区间条 / 较上次变化 / 变动归因都能出内容。"""
    return _base_fixture({})


@pytest.fixture
def fixture_evidence():
    """关联性证据：4 张图 + 数据表 + 方向校验 + caveats。"""
    return _base_fixture({
        "/api/thesis/dividend_env/evidence?factor=premium": _evidence_payload(),
        "/api/thesis/dividend_env/evidence?factor=insurance_gap":
            _evidence_unsupported_payload(),
        "/api/thesis/dividend_env/evidence?factor=style_rel": _evidence_short_payload(),
    })


@pytest.fixture
def fixture_report():
    return _base_fixture({})


@pytest.fixture
def fixture_report_chart():
    """报告并图：/report 与 /chart 都可用，报告里必须画出 #thReportScore。"""
    return _base_fixture({
        "/api/thesis/dividend_env/chart?force=0&points=1200": _chart_payload(),
    })


@pytest.fixture
def fixture_report_chart_404():
    """后端 /chart 还没装配：报告仍要成功导出，只把双轴图那节隐藏并写原因。"""
    return _base_fixture({
        "/api/thesis/dividend_env/chart?force=0&points=1200": {"detail": "Not Found"},
    })


@pytest.fixture
def fixture_chart():
    """双轴图：总览 2 张小图 + 详情大图共用同一份桩数据（含切台账用的第二张台账）。

    dividend_env 热缓存（cached=true, cache_age_s=600 → 「缓存 10 分钟前」）；
    gold_regime 刚重算（cached=false → 「刚重算」），用来覆盖两种新鲜度文案。
    force=1 的 URL 单独给桩：总览「↻ 重新拉取」会强制刷新。
    """
    items = [_list_item("dividend_env", "红利环境因子包", "中证红利低波"),
             _list_item("gold_regime", "黄金阶段研判台账", "黄金")]
    return _base_fixture({
        "/api/thesis/list": _list_payload(items),
        "/api/thesis/dividend_env/chart?force=0&points=1200": _chart_payload(),
        "/api/thesis/dividend_env/chart?force=1&points=1200":
            _chart_payload(cached=False, cache_age_s=None),
        "/api/thesis/gold_regime/chart?force=0&points=1200":
            _chart_payload("gold_regime", "黄金阶段研判台账", version="2.0",
                           def_hash="h2", cached=False, cache_age_s=None),
        "/api/thesis/gold_regime/chart?force=1&points=1200":
            _chart_payload("gold_regime", "黄金阶段研判台账", version="2.0",
                           def_hash="h2", cached=False, cache_age_s=None),
        "/api/thesis/gold_regime": _gold_detail_payload(),
        "/api/thesis/gold_regime/history?days=180": _history_with_values(6),
    })


@pytest.fixture
def fixture_chart_version():
    """同一台账连续三次返回不同定义：第一次只记录、第二次提示版本、第三次提示内容变更。"""
    items = [_list_item("dividend_env", "红利环境因子包", "中证红利低波")]
    return _base_fixture({
        "/api/thesis/list": _list_payload(items),
        "/api/thesis/dividend_env/chart?force=0&points=1200": [
            _chart_payload(def_hash="h1", version="1.0"),
            _chart_payload(def_hash="h2", version="1.1"),
            _chart_payload(def_hash="h3", version="1.1"),
        ],
    })


@pytest.fixture
def fixture_chart_version_seeded():
    """localStorage 里已有旧记录（模拟刷新前定义被改过）：首次加载就要提示。"""
    return _base_fixture({
        "/api/thesis/dividend_env/chart?force=0&points=1200":
            _chart_payload(def_hash="h2", version="1.1"),
    })


class TestDetailRender:
    def test_renders_without_runtime_error(self, tmp_path, fixture_single_point):
        """回归：Math.abs(unit).indexOf(...) 这类运行时类型错误必须被这里拦住。"""
        html = _render(tmp_path, fixture_single_point)
        assert "① 股息率溢价" in html
        assert "② 10Y 国债" in html

    def test_unit_and_value_formatting(self, tmp_path, fixture_single_point):
        html = _render(tmp_path, fixture_single_point)
        assert "亿元" in html
        assert ">15000<" in html          # 亿元走整数
        assert "1.69" in html             # 百分比走两位小数
        assert "+17.65" in html           # 相对收益带正号
        assert "%偏离" in html

    def test_score_badges_and_missing_factor(self, tmp_path, fixture_single_point):
        html = _render(tmp_path, fixture_single_point)
        assert 'class="th-score s2"' in html     # 很有利
        assert 'class="th-score s1"' in html     # 有利
        assert 'class="th-score s0"' in html     # 中性
        assert 'class="th-score sn"' in html     # 数据缺失（不是 0 分）
        assert "数据缺失" in html

    def test_stale_note_and_source(self, tmp_path, fixture_single_point):
        html = _render(tmp_path, fixture_single_point)
        assert "数据可能滞后" in html
        assert "华泰测算欠配" in html

    def test_manual_factor_gets_badge_and_highlight(self, tmp_path, fixture_single_point):
        """手工项必须在页面上可识别：卡片高亮 + 「手工维护」徽标。

        这是**可识别性**标记，不是降权或警示——手工项的分数照常计入总分，
        只是要让读者知道这一条不是当日自动取到的数据。
        """
        html = _render(tmp_path, fixture_single_point)
        assert "is-manual" in html
        assert "手工维护" in html
        assert html.count("is-manual") == 1, "只有标注 manual 的因子才该被高亮"

    def test_pct_rank_band_and_confidence_badge(self, tmp_path, fixture_single_point):
        """历史分位带与置信度徽标。

        分位带必须真的画出来（有 track）；未给 pct_rank 的因子显示「样本不足」，
        而不是画一条没有意义的空条——报告反复强调 n 小则统计量只能看方向。
        """
        html = _render(tmp_path, fixture_single_point)
        assert "th-pct-track" in html
        assert "历史分位" in html
        assert "样本不足" in html
        assert "th-conf-high" in html
        assert "置信度高" in html

    def test_scenarios_phases_transmission(self, tmp_path, fixture_single_point):
        """研究表达层三块：情景推演（命中高亮 + 条件带当前值）、阶段定位、传导链路。"""
        html = _render(tmp_path, fixture_single_point)
        assert "情景推演" in html
        assert "th-scen is-cur" in html          # 命中的情景必须高亮
        assert "当前" in html
        assert "th-phase is-cur" in html         # 当前阶段必须高亮
        assert "历史阶段定位" in html
        assert "传导链路" in html
        assert "当前生效" in html
        # 条件要带出当前值，让人看得见「差哪一条」
        assert "1.6862%" in html

    def test_stages_block_absent_for_old_theses(self, tmp_path, fixture_single_point):
        """没声明 stages 的台账（红利/恒科/债基）不得出现阶段矩阵块或占位文案。"""
        html = _render(tmp_path, fixture_single_point)
        assert "阶段定位矩阵" not in html
        assert "th-stg-matrix" not in html

    def test_market_matrix_heatmap_and_correlation(self, tmp_path, fixture_single_point):
        """热力图与相关性矩阵必须渲染出单元格底色。

        配色是 A 股习惯：涨/正相关=红、跌/负相关=绿——断言两个 rgba 都在，
        单纯断言表格结构抓不到「颜色反了」这类错误。
        """
        html = _render(tmp_path, fixture_single_point)
        assert "th-matrix" in html
        assert "资产 × 时间窗口" in html
        assert "相关性矩阵" in html
        assert "rgba(192,57,43" in html          # 红：涨 / 正相关
        assert "rgba(30,132,73" in html          # 绿：跌 / 负相关

    def test_signals_invalidation_and_alerts(self, tmp_path, fixture_single_point):
        html = _render(tmp_path, fixture_single_point)
        assert "险资欠配 &gt; 1 万亿元" in html    # 已转义
        assert "股息率溢价跌破 1.5pp" in html
        assert "已命中 1 条" in html
        assert "结论失效条件命中" in html
        assert 'class="th-alert-sev critical"' in html
        assert 'class="th-alert-sev warn"' in html
        assert 'class="th-alert-sev info"' in html
        assert "严重 1 · 注意 1 · 提示 1" in html

    def test_warnings_block(self, tmp_path, fixture_single_point):
        html = _render(tmp_path, fixture_single_point)
        assert "数据提示" in html
        assert "已超 120 天" in html


class TestStageMatrixRender:
    """黄金台账的阶段矩阵：九宫格 + 双轴读数 + 位置维度不画分位带。"""

    def test_matrix_renders_nine_cells(self, tmp_path, fixture_gold):
        html = _render(tmp_path, fixture_gold, "gold_regime")
        assert "阶段定位矩阵" in html
        assert "th-stg-matrix" in html
        for name in ("低位积累期", "顺风上行期", "趋势延伸期", "低位磨底期",
                     "中性震荡期", "高位僵持期", "逆风错杀期", "逆风调整期",
                     "高位风险期"):
            assert name in html, name

    def test_current_cell_is_highlighted_with_tone_class(self, tmp_path, fixture_gold):
        """当前格必须高亮且带方向色类；只有一格是当前格。

        配色遵循 A 股习惯（有利=红、不利=绿），故 tone 必须真的落到 class 上——
        只断言「格子都在」抓不到「九宫格全一个颜色」这类错误。
        """
        html = _render(tmp_path, fixture_gold, "gold_regime")
        assert 'th-stg-c th-tone-neutral is-cur' in html
        assert html.count("is-cur") == 1, "九宫格里有且只有一格是当前格"
        # 两种轴标签（驱动档 / 位置档）都要出现
        for axis in ("顺风", "中性", "逆风", "低位", "中性位", "高位"):
            assert axis in html, axis

    def test_drive_and_position_readouts_use_their_own_scales(self, tmp_path, fixture_gold):
        """驱动满分按 11 显示（⑥ 只给 ±1），不能按「6 因子 × 2 = 12」标。"""
        html = _render(tmp_path, fixture_gold, "gold_regime")
        assert ">+1</b>" in html
        assert "±11" in html
        assert "±12" not in html

    def test_position_factor_skips_the_pct_rank_band(self, tmp_path, fixture_gold):
        """⑦ 的读数本身就是分位 → 跳过「历史分位」带，其余因子照常显示。

        夹具里 7 个因子的 pct_rank 都是 null，其中 6 个显示「样本不足」占位，
        只有 ⑦（`pct_rank_off`）整条不渲染——数条数比断言「文案不存在」精确，
        否则夹具一改（给某因子补上分位）测试就会假红。
        """
        html = _render(tmp_path, fixture_gold, "gold_regime")
        assert "⑦ 价格位置（延伸度分位）" in html
        assert html.count('class="th-pct') == 6, "应只有 ⑦ 不画分位带"


class TestStageMatrixRender:
    """黄金台账的阶段矩阵：九宫格 + 双轴读数 + 位置维度不画分位带。"""

    def test_matrix_renders_nine_cells(self, tmp_path, fixture_gold):
        html = _render(tmp_path, fixture_gold, "gold_regime")
        assert "阶段定位矩阵" in html
        assert "th-stg-matrix" in html
        for name in ("低位积累期", "顺风上行期", "趋势延伸期", "低位磨底期",
                     "中性震荡期", "高位僵持期", "逆风错杀期", "逆风调整期",
                     "高位风险期"):
            assert name in html, name

    def test_current_cell_is_highlighted_with_tone_class(self, tmp_path, fixture_gold):
        """当前格必须高亮且带方向色类；只有一格是当前格。

        配色遵循 A 股习惯（有利=红、不利=绿），故 tone 必须真的落到 class 上——
        只断言「格子都在」抓不到「九宫格全一个颜色」这类错误。
        """
        html = _render(tmp_path, fixture_gold, "gold_regime")
        assert 'th-stg-c th-tone-neutral is-cur' in html
        assert html.count("is-cur") == 1, "九宫格里有且只有一格是当前格"
        # 两种轴标签（驱动档 / 位置档）都要出现
        for axis in ("顺风", "中性", "逆风", "低位", "中性位", "高位"):
            assert axis in html, axis

    def test_drive_and_position_readouts_use_their_own_scales(self, tmp_path, fixture_gold):
        """驱动满分按 11 显示（⑥ 只给 ±1），不能按「6 因子 × 2 = 12」标。"""
        html = _render(tmp_path, fixture_gold, "gold_regime")
        assert ">+1</b>" in html
        assert "±11" in html
        assert "±12" not in html

    def test_position_factor_skips_the_pct_rank_band(self, tmp_path, fixture_gold):
        """⑦ 的读数本身就是分位 → 跳过「历史分位」带，其余因子照常显示。

        夹具里 7 个因子的 pct_rank 都是 null，其中 6 个显示「样本不足」占位，
        只有 ⑦（`pct_rank_off`）整条不渲染——数条数比断言「文案不存在」精确，
        否则夹具一改（给某因子补上分位）测试就会假红。
        """
        html = _render(tmp_path, fixture_gold, "gold_regime")
        assert "⑦ 价格位置（延伸度分位）" in html
        assert html.count('class="th-pct') == 6, "应只有 ⑦ 不画分位带"


class TestChartRender:
    def test_multi_point_history_builds_chart_options(self, tmp_path, fixture_with_chart):
        """2 个以上历史点会走图表分支——tooltip formatter 与 series 取数一起被跑到。"""
        html = _render(tmp_path, fixture_with_chart)
        assert "历史趋势" in html
        assert "① 股息率溢价" in html


class TestHistoryAndAttribution:
    """历史快照派生层：sparkline / 历史区间条 / 较上次变化 / 变动归因。"""

    def test_sparkline_and_range_bar_from_history_values(self, tmp_path, fixture_rich):
        """有 24 期 values 时必须画折线 + 分位区间条，而不是「样本不足」占位。

        数据来源是 /history 的 items[].values；不许为每个因子建 ECharts 实例
        （用 SVG sparkline），所以断言的是内联 SVG 而不是图表容器。
        """
        html = _render(tmp_path, fixture_rich)
        assert "th-spark-line" in html
        assert "<svg" in html
        assert html.count("th-spark-line") >= 5      # 6 个因子里只有「数据缺失」那条没序列
        assert "th-range-track" in html
        assert "th-range-cur" in html          # 当前值标记
        assert "期" in html                     # 「历史分位 x% · 24 期」
        assert "历史分位 78%" not in html        # 卡片分位来自本地 history，不是证据里的 0.78
        assert html.count("th-spark-empty") == 1, "只有缺数据的那条因子该显示空占位"

    def test_change_chip_uses_value_delta_with_unit(self, tmp_path, fixture_rich):
        """chip 是 values 差值 + 单位（+0.18pp / -5.3bp），不是分数差。"""
        html = _render(tmp_path, fixture_rich)
        assert "较上次" in html
        assert "+0.02pp" in html               # premium 每期 +0.02pp

    def test_attribution_note_and_bar(self, tmp_path, fixture_rich):
        """变动归因必须同时给出「总分 X→Y（Δ）」与逐因子 Δ，并画横向条形图。"""
        html = _render(tmp_path, fixture_rich)
        assert "变动归因" in html
        assert "总分 0 → +1（+1）" in html
        assert "① 股息率溢价 0 → +1（+1）" in html
        assert "② 10Y 国债 +1 → 0（-1）" in html
        assert "其余不变" in html
        assert 'id="thAttrChart"' in html

    def test_single_point_shows_placeholder_not_fake_chart(self, tmp_path):
        """只有 1 个历史点：给占位与归档口径说明，绝不画一张空图。"""
        fx = _base_fixture({
            "/api/thesis/dividend_env/history?days=180": _history_payload(1),
        })
        html = _render(tmp_path, fx)
        assert "只有 1 个历史点" in html
        assert "同一天重复刷新只覆盖不新增" in html
        assert 'id="thAttrChart"' not in html


class TestEvidencePanel:
    """「关联性证据」折叠区：2×2 图 + 数据表 + 方向校验 + caveats。"""

    def test_four_charts_table_direction_and_caveats(self, tmp_path, fixture_evidence):
        html = _run(tmp_path, fixture_evidence, "dividend_env", "evidence", "premium")
        for title in ("① 走势对照", "② 散点 + 拟合线", "③ 五分位未来收益", "④ 滚动相关"):
            assert title in html, title
        # dual 于 2026-11-01 改为**双坐标轴**（左=因子读数、右=标的）：标准化虽然能让
        # 两条线共用一个刻度，但读不到「因子实际读数 / 标的实际点位」，而且波动大的因子
        # （资金面 Shibor vs 债券指数就是典型）会把标的压成一条平线，看不出拐点对应关系。
        assert "左轴 因子 / 右轴 标的" in html
        assert "标准化（100 ± 10×标准差" not in html
        assert "归一化到 100" not in html
        # 有效 n（effective_n）优先，观测总数只作附注
        assert "有效 n=220" in html
        assert "观测 300" in html
        assert "真正进入统计" in html
        assert "Q5−Q1" in html
        assert "当前桶" in html
        assert "+0.21" in html                  # corr_value
        assert "210" in html                    # n
        assert "方向校验" in html
        assert "方向一致" in html
        assert "相关不等于因果" in html          # caveats
        assert "未来收益窗口重叠" in html
        assert 'id="evc-premium-dual"' in html
        assert 'id="evc-premium-scatter"' in html
        assert 'id="evc-premium-layers"' in html
        assert 'id="evc-premium-rolling"' in html

    def test_short_window_horizon_is_flagged(self, tmp_path, fixture_evidence):
        """horizons 末尾的短窗口（short_window）必须标出来，期限标签保持真实 h。

        否则读者会把 h=5 的 r 当成 20 日主口径（2026-10-27 契约变化）。
        """
        html = _run(tmp_path, fixture_evidence, "dividend_env", "evidence", "style_rel")
        assert "短窗口参考" in html
        assert "未来 5 个交易日" in html
        assert "+0.63" in html
        assert "仅作方向参考" in html
        assert "未来 20 个交易日" not in html   # 不能把短窗口硬写成 20

    def test_unsupported_factor_shows_reason_and_accumulation(self, tmp_path, fixture_evidence):
        """手工/不可回放因子：显示原因 + 「积累中 n/30」，不画假图。"""
        html = _run(tmp_path, fixture_evidence, "dividend_env", "evidence", "insurance_gap")
        assert "手工维护因子" in html
        assert "积累中 10/30 期" in html
        assert "不做假图" in html
        assert 'class="th-ev-grid"' not in html

    def test_two_open_factors_share_tabs_and_do_not_reflow_cards(
            self, tmp_path, fixture_evidence):
        """同时打开两个（不相邻）因子：只新增标签，卡片网格不重排。

        旧实现把详情塞回卡片并让该卡 `grid-column:1/-1`，会把它后面的卡挤下去；
        现在统一停靠区 + 标签，打开 N 个页面只增高一次，且只渲染当前标签。
        """
        data = json.loads(_run(tmp_path, fixture_evidence, "dividend_env", "evidence_tabs"))
        assert 'data-ev-tab="premium"' in data["tabs"]
        assert 'data-ev-tab="style_rel"' in data["tabs"]
        assert "已打开 2 个" in data["count"]
        # 当前标签是后打开的 style_rel（短窗口 payload），停靠区显示其内容
        assert "短窗口参考" in data["body"]
        assert data["dockDisplay"] == "flex"
        # 卡片本身不承载 ev-<key> 容器（不再就地展开）
        assert "ev-premium" not in data["body"]


class TestReportImage:
    """报告图片：/report → 离屏 1240px 容器 → SVG renderer 3 张图 → PNG → 预览。"""

    def test_report_generates_svg_charts_png_and_cleans_up(self, tmp_path, fixture_report):
        out = _run(tmp_path, fixture_report, "dividend_env", "report")
        data = json.loads(out)

        # 3 张报告图必须走 SVG renderer（html-to-image 对 canvas 支持很差）
        assert {"thReportTrend", "thReportAttr", "thReportHeat"}.issubset(
            set(data["svgCharts"])), data["svgCharts"]
        # 离屏容器生成后必须移除，不能残留在 body 里影响主页面
        assert data["reportRemoved"] is True
        # 预览弹窗 + 文件名
        assert data["preview"] is True
        assert data["pixelRatio"] == 2
        assert data["exportWidth"] == 1240
        assert "研究台账_" in data["previewHtml"]

        html = data["reportHtml"]
        for text in ("研究台账 · 红利环境因子包", "总分", "有效因子", "最近变动",
                     "历史表现", "数据证明", "滚动相关", "结论边界", "仅供研究",
                     "中证红利低波", "全收益口径"):
            assert text in html, text
        assert "股息率溢价" in html
        assert "方向一致" in html
        assert "手工维护因子" in html            # 不可回放因子在总表里给原因
        assert "th-rp-table" in html
        assert "th-rp-chart" in html
        # corr/n 缺失时方向列不许写成「正相关（方向一致）」，要明确「样本不足」
        assert "—（样本不足）" in html
        # 热力图只画近 3 年；窗口外的老因子要注明跳过，而不是留一片空白
        assert "近 3 年" in html
        assert "已跳过" in html
        assert "⑥ M1-M2 剪刀差" in html
        # 短窗口行（h=5）必须带角标与脚注，不能与 20 日主口径混看
        assert "th-rp-short" in html
        assert "短窗口" in html
        assert "不要与常规期限的 r 直接比较" in html

    def test_heatmap_window_drops_pre_3y_rows(self):
        """热力图窗口 = 最新日期往前推 3 年；整体越界的因子被跳过。"""
        js = (ROOT / "server" / "static" / "js" / "thesis.js").read_text(encoding="utf-8")
        assert "_heatWindow" in js
        assert "近 3 年" in js
        assert "已跳过" in js

    def test_report_includes_dual_axis_chart(self, tmp_path, fixture_report_chart):
        """报告 PNG 必须包含「评分 vs 标的」双轴图：导出前 init + setOption 已完成。"""
        data = json.loads(_run(tmp_path, fixture_report_chart, "dividend_env", "report"))
        html = data["reportHtml"]
        assert 'id="thReportScore"' in html
        assert "评分 vs 标的" in html
        assert "双轴各自自适应" in html
        # 口径说明：内样本形状核对，别让读者当成样本外验证
        assert "历史重放（内样本）" in html
        assert "仅形状核对" in html
        # 导出前已经画了图，且与其余三张报告图一样走 SVG renderer（html-to-image 才截得到）
        assert "thReportScore" in data["svgCharts"], data["svgCharts"]
        assert data["scoreInit"] is True
        assert data["scoreDisposed"] == 1, "报告容器移除时双轴图实例必须被释放"
        assert data["scoreNoteDisplay"] == "none"
        assert data["preview"] is True
        assert data["reportRemoved"] is True

    def test_report_survives_chart_404(self, tmp_path, fixture_report_chart_404):
        """后端 /chart 未装配：只隐藏双轴图那节并写原因，整份报告仍要能导出。"""
        data = json.loads(_run(tmp_path, fixture_report_chart_404, "dividend_env", "report"))
        assert data["preview"] is True, "并图失败不能让整份报告失败"
        assert data["reportRemoved"] is True
        assert data["scoreInit"] is False
        assert data["scoreDisplay"] == "none"
        assert data["scoreNoteDisplay"] == ""
        assert "图表服务未就绪" in data["scoreNote"]
        assert {"thReportTrend", "thReportAttr", "thReportHeat"}.issubset(
            set(data["svgCharts"])), data["svgCharts"]


class TestDualAxisChart:
    """「评分 vs 标的」双轴图：总览小图 + 详情大图 + 接口未就绪降级 + 生命周期。"""

    def test_overview_cards_metrics_and_charts(self, tmp_path, fixture_chart):
        data = json.loads(_run(tmp_path, fixture_chart, "dividend_env", "chart_overview"))
        assert data["panel"] == "flex"
        assert 'data-tid="dividend_env"' in data["grid"]
        assert 'data-tid="gold_regime"' in data["grid"]
        assert "红利环境因子包" in data["grid"]
        assert data["vers"][0] == "v1.0", data["vers"]
        assert "ρ水平" in data["rhos"][0]
        assert "+0.15" in data["rhos"][0]       # r_fwd
        assert "+0.23" in data["rhos"][0]       # r_fwd_flip
        assert data["notes"][0]["display"] == "none"   # 出图后隐藏「加载中…」
        assert data["meta"] == "已加载 2/2", data["meta"]
        assert len(data["charts"]) == 2, data["charts"]
        # 缓存新鲜度：热缓存显示「缓存 10 分钟前」，刚重算显示「刚重算」
        assert data["fresh"][0] == "数据 2026-09-06 · 缓存 10 分钟前", data["fresh"]
        assert "刚重算" in data["fresh"][1], data["fresh"]
        # 首次见（本地没有任何记录）不许报告「定义已更新」
        assert data["dots"] == ["none", "none"], data["dots"]

    def test_overview_degrades_to_not_ready(self, tmp_path):
        """后端 /chart 还没装配：每张卡显示「图表服务未就绪」，不抛异常、不白屏。"""
        fx = _base_fixture({
            "/api/thesis/list": _list_payload([
                _list_item("dividend_env", "红利环境因子包"),
                _list_item("gold_regime", "黄金阶段研判台账"),
            ]),
            "/api/thesis/dividend_env/chart?force=0&points=1200": {"detail": "Not Found"},
        })
        data = json.loads(_run(tmp_path, fx, "dividend_env", "chart_overview"))
        assert "图表服务未就绪" in data["notes"][0]["text"]
        assert data["notes"][0]["display"] == ""      # 占位可见（不是白屏）
        assert data["charts"] == []
        assert "2 张未就绪" in data["meta"]

    def test_detail_card_subtitle_and_chart(self, tmp_path, fixture_chart):
        data = json.loads(_run(tmp_path, fixture_chart, "dividend_env", "chart_detail"))
        assert data["chartShown"] is True
        assert "v1.0" in data["sub"]
        assert "数据 2026-09-01~2026-09-06" in data["sub"]
        assert "ρ水平" in data["sub"] and "+0.15" in data["sub"]
        # 缓存新鲜度：后端 1h 缓存命中时把「多久前算的」写进副标题
        assert "缓存 10 分钟前" in data["sub"], data["sub"]
        assert data["noteDisplay"] == "none"
        # 首次见不提示定义变更（本地没有记录）
        assert data["notice"] == ""
        assert data["noticeDisplay"] == "none"

    def test_detail_degrades_to_not_ready(self, tmp_path):
        data = json.loads(_run(tmp_path, _base_fixture({}), "dividend_env", "chart_detail"))
        assert data["chartShown"] is False
        assert "图表服务未就绪" in data["note"]
        assert data["noteDisplay"] == ""

    def test_close_panel_disposes_mini_charts(self, tmp_path, fixture_chart):
        """关闭总览必须 dispose 全部小图并清空 grid（隐藏容器里不许堆图）。"""
        data = json.loads(_run(tmp_path, fixture_chart, "dividend_env", "chart_lifecycle"))
        assert data["before"] == 2
        assert data["after"] == 0
        assert data["disposed"] == 2
        assert data["grid"] == ""
        assert data["panel"] == "none"

    def test_switch_and_close_page_dispose_detail_chart(self, tmp_path, fixture_chart):
        data = json.loads(_run(tmp_path, fixture_chart, "dividend_env", "chart_switch"))
        assert data["activeAfterSwitch"] == 1, "切台账后详情大图只该留一个实例"
        assert data["afterClose"] == 0, "关页后不许残留图表实例"
        assert data["disposed"] >= 2

    def test_detail_html_contains_dual_axis_card(self, tmp_path, fixture_single_point):
        html = _render(tmp_path, fixture_single_point)
        assert 'id="thScoreChart"' in html
        assert "评分 vs 标的（双轴）" in html
        # 这张卡是 .th-main（flex column）直接子项，必须带不可收缩类，否则被压成 0 高
        assert "th-score-card" in html
        # 版本变更 chip 与副标题同级（默认隐藏，取到数据且 def_hash 变过才亮）
        assert 'id="thScoreChartNotice"' in html

    def test_reload_button_forces_refresh(self, tmp_path, fixture_chart):
        """总览「↻ 重新拉取」必须 force=1（跳过后端 1h 缓存）；首次打开仍是 force=0。"""
        data = json.loads(_run(tmp_path, fixture_chart, "dividend_env", "chart_force"))
        chart_urls = [u for u in data["fetches"] if "/chart?" in u]
        forced = [u for u in chart_urls if "force=1" in u]
        assert forced, chart_urls
        assert len(forced) >= 2, "总览每张台账都要带 force=1"
        assert all("points=1200" in u for u in forced), forced

    def test_version_notice_first_silent_then_chips(self, tmp_path, fixture_chart_version):
        """连续三次进详情：第一次只记录；hash+version 变→版本 chip；hash 变→内容 chip。"""
        data = json.loads(_run(tmp_path, fixture_chart_version, "dividend_env",
                               "chart_version"))
        rounds = data["rounds"]
        assert rounds[0]["notice"] == "", "首次见只记录，不许误报"
        assert rounds[0]["display"] == "none"
        assert rounds[1]["notice"] == "定义已更新 v1.0→v1.1，图已重算", rounds[1]
        assert rounds[1]["display"] == ""
        assert "定义内容已更新（阈值/因子）" in rounds[2]["notice"], rounds[2]
        # 记录更新到最新定义：下次刷新不再重复报
        assert data["store"]["dividend_env"] == {"def_hash": "h3", "def_version": "1.1"}
        assert data["writes"], "记录必须落到 localStorage（跨刷新保留）"

    def test_version_notice_survives_page_reload(self, tmp_path, fixture_chart_version_seeded):
        """localStorage 已有旧记录：刷新后首次加载就提示，随后不再重复。"""
        data = json.loads(_run(tmp_path, fixture_chart_version_seeded, "dividend_env",
                               "chart_version_seeded"))
        assert data["rounds"][0]["notice"] == "定义已更新 v1.0→v1.1，图已重算"
        assert data["rounds"][1]["notice"] == ""
        assert data["rounds"][2]["notice"] == ""

    def test_version_notice_without_localstorage(self, tmp_path, fixture_chart_version):
        """隐私模式 localStorage 访问即抛：退化为内存，仍能提示且详情渲染不失败。"""
        data = json.loads(_run(tmp_path, fixture_chart_version, "dividend_env",
                               "chart_version_nostore"))
        assert data["store"] is None, "桩里 localStorage 不可用，读不到记录是预期"
        assert data["rounds"][0]["notice"] == ""
        assert "定义已更新 v1.0→v1.1" in data["rounds"][1]["notice"]
        assert "定义内容已更新" in data["rounds"][2]["notice"]
        assert data["rounds"][1]["sub"], "详情副标题必须照常渲染"


class TestStaticWiring:
    """静态接线：按钮/版本号/导出路径/样式类——这些抓「改了逻辑忘了接线」。"""

    INDEX = ROOT / "server" / "static" / "index.html"
    CHAT_EXPORT = ROOT / "server" / "static" / "js" / "chat-export.js"
    CSS = ROOT / "server" / "static" / "css" / "thesis.css"

    def test_report_button_and_asset_versions(self):
        html = self.INDEX.read_text(encoding="utf-8")
        assert 'id="thReportBtn"' in html
        assert "生成报告" in html
        # 每次改静态资源都要递增 ?v=N（契约对齐后又递增了一次；双轴图新增再递增）
        assert "thesis.css?v=22" in html
        # thesis.js 在 555058c3 改过但当时漏升版本（v=29 的 immutable 缓存会让老浏览器拿旧文件）；
        # 2026-11-01 补升到 v=30，并纳入 test_static_asset_versions.py 的通用守卫；
        # 双轴总览/详情大图升到 v=31；报告并图 + 版本提示 + 缓存文案再升到 v=32；
        # 2026-11-05 报告视图语义/色板收敛升到 v=35（v=33 是中间态，已随 revert 作废）。
        assert "thesis.js?v=35" in html
        assert "chat-export.js?v=27" in html

    def test_chart_overview_and_dual_axis_wiring(self):
        """总览入口/面板/详情容器，以及「两轴各自 scale、共用一份图配置」的接线。"""
        html = self.INDEX.read_text(encoding="utf-8")
        assert 'id="thChartBtn"' in html
        assert "◫ 双轴总览" in html
        assert 'id="thChartPanel"' in html
        assert 'id="thChartGrid"' in html
        assert 'id="thChartClose"' in html
        js = (ROOT / "server/static/js/thesis.js").read_text(encoding="utf-8")
        assert "/chart?force=" in js
        assert "points=1200" in js
        assert "图表服务未就绪" in js
        assert "SCORE_MAX_CONC" in js
        assert "openScorePanel" in js
        # 「↻ 重新拉取」= 强制刷新：必须传 openScorePanel(true) → /chart?force=1
        assert "openScorePanel(true)" in js
        # 定义版本提示：localStorage key 固定（改 key 等于让所有用户重置一次「已见」记录）
        assert "sr_thesis_chart_ver_v1" in js
        assert "CHART_VER_KEY" in js
        assert "定义已更新 v" in js
        assert "定义内容已更新（阈值/因子）" in js
        # 报告并图容器与缓存新鲜度文案
        assert 'id="thReportScore"' in js
        assert "缓存 " in js and "分钟前" in js
        assert "刚重算" in js
        # 两处复用同一个图配置函数（不是各写一份），且两轴各自 scale 自适应
        assert js.count("function _scoreChartOption") == 1
        assert "step: 'end'" in js
        assert "yAxisIndex: 1" in js
        assert js.count("scale: true") >= 2
        css = self.CSS.read_text(encoding="utf-8")
        grid = css.split(".th-chart-grid {", 1)[1].split("}", 1)[0]
        assert "align-content: start" in grid, "grid 默认 stretch 会把行高撑出空白"
        card = css.split(".th-score-card {", 1)[1].split("}", 1)[0]
        assert "flex: none" in card, "详情大图卡在 flex 列里必须不可收缩"

    def test_element_to_png_blob_exported_and_reuses_guards(self):
        js = self.CHAT_EXPORT.read_text(encoding="utf-8")
        assert "async function elementToPngBlob" in js
        assert "elementToPngBlob," in js          # 挂在 return 对象上
        assert "ensureHtmlToImage()" in js
        assert "applyExportGuardStyle()" in js
        # 报告容器不能用负坐标离屏：html-to-image 会把内容裁成空白图
        assert "position:fixed;left:0;top:0" in (ROOT / "server" / "static" / "js"
                                                 / "thesis.js").read_text(encoding="utf-8")
        assert "z-index:-9999" in (ROOT / "server" / "static" / "js"
                                   / "thesis.js").read_text(encoding="utf-8")

    def test_thesis_js_report_pipeline(self):
        js = (ROOT / "server" / "static" / "js" / "thesis.js").read_text(encoding="utf-8")
        assert "renderer: 'svg'" in js
        assert "elementToPngBlob" in js
        assert "/report" in js
        assert "研究台账_" in js
        assert "thReportBtn" in js
        assert "getReportBtn" not in js
        # 30 分钟前端 PNG 缓存 + 强制重算 + 复制图片
        assert "REPORT_CACHE_TTL_MS" in js
        assert "?force=1" in js
        assert 'data-act="copy"' in js
        assert "ClipboardItem" in js

    def test_css_has_new_component_styles(self):
        css = self.CSS.read_text(encoding="utf-8")
        for sel in (".th-spark", ".th-range-track", ".th-chg", ".th-attr-chart",
                    ".th-ev-grid", ".th-factor.is-ev-open", ".th-report-modal",
                    ".th-rp-table", ".th-rp-chart", ".th-rp-caliber",
                    ".th-ev-dock", ".th-ev-tabs",
                    ".th-chart-panel", ".th-chart-grid", ".th-chart-card",
                    ".th-chart-card-dot", ".th-chart-card-fresh", ".th-chart-notice",
                    ".th-chart-mini", ".th-score-chart", ".th-score-card"):
            assert sel in css, sel

    def test_factor_grid_does_not_stretch_cards(self):
        """因子卡必须按自身内容高度排列（grid 默认 stretch 会把整行拉到最高那张）。"""
        css = self.CSS.read_text(encoding="utf-8")
        block = css.split(".th-factors {", 1)[1].split("}", 1)[0]
        assert "align-items: start" in block
        # 展开证据的卡不再横跨整行（详情在统一停靠区，不能靠 grid-column 抢行）
        is_open = css.split(".th-factor.is-ev-open", 1)[1].split("}", 1)[0]
        assert "grid-column" not in is_open
        # 详情区是 fixed 右侧抽屉：不参与因子网格排版，不挤卡片
        dock = css.split(".th-ev-dock {", 1)[1].split("}", 1)[0]
        assert "position: fixed" in dock
        assert "grid-column" not in dock
