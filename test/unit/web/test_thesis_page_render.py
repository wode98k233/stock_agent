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

function makeEl(id) {
  return {
    id, innerHTML: '', textContent: '', style: {}, dataset: {}, disabled: false,
    classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
                 contains(c) { return this._s.has(c); } },
    addEventListener() {},
    querySelectorAll() { return []; },
  };
}
const els = {};
const getEl = (id) => (els[id] || (els[id] = makeEl(id)));

let formatterError = null;
const echartsStub = {
  init() {
    return {
      setOption(opt) {
        // 顺手把 tooltip formatter 跑一遍：这是图表里唯一带索引取数的逻辑
        try {
          const ps = [{ dataIndex: 0 }];
          opt.tooltip.formatter(ps);
        } catch (e) { formatterError = e; }
        if (opt.series && opt.series[0] && typeof opt.series[0].data.map !== 'function') {
          formatterError = new Error('series.data 不是数组');
        }
        if (opt.xAxis && typeof opt.xAxis.data.map !== 'function') {
          formatterError = new Error('xAxis.data 不是数组');
        }
        if (opt.series[0] && opt.series[0].markLine) {
          try { opt.series[0].markLine.data.map((d) => d.yAxis); }
          catch (e) { formatterError = e; }
        }
      },
      dispose() {},
      resize() {},
    };
  },
};

const document = {
  getElementById: getEl,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: (t) => makeEl(t),
  head: { appendChild() {} },
  body: { appendChild() {} },
};
const windowStub = {};

global.document = document;
global.window = windowStub;
global.echarts = echartsStub;
windowStub.echarts = echartsStub;
global.requestAnimationFrame = (fn) => setTimeout(fn, 0);
global.fetch = (url) => {
  const u = String(url);
  const body = fx[u.replace(/^https?:\/\/[^/]+/, '')] || fx['__default__'];
  if (!body) return Promise.reject(new Error('no fixture for ' + u));
  return Promise.resolve({ json: () => Promise.resolve(body) });
};

vm.runInThisContext(pageJs, { filename: 'thesis.js' });

const api = windowStub.StockRadar && windowStub.StockRadar.thesis;
if (!api) { console.error('NO_API'); process.exit(4); }

api.loadDetail(process.env.THESIS_ID || 'dividend_env').then(() => {
  // 组合建议与市场矩阵是 renderDetail 里 fire-and-forget 的异步块，
  // 必须让出事件循环等它们落地，否则断言的是「还没渲染」的 DOM。
  return new Promise((resolve) => setTimeout(resolve, 120));
}).then(() => {
  if (formatterError) {
    console.error('CHART_ERROR: ' + formatterError.message);
    process.exit(5);
  }
  process.stdout.write(getEl('thMain').innerHTML
    // 市场矩阵是异步填充到独立容器的：桩 DOM 不会把它合并进 thMain.innerHTML，
    // 这里显式拼上，否则断言的是「还没渲染」。
    + getEl('thMatrix').innerHTML);
}).catch((e) => {
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


def _render(tmp_path, fixture: dict, thesis_id: str = "dividend_env") -> str:
    node = _node_bin()
    if not node:
        pytest.skip("未找到 node，跳过前端渲染冒烟测试")

    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    fx = tmp_path / "fixture.json"
    fx.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")

    env = dict(os.environ, THESIS_JS=str(PAGE_JS), THESIS_FIXTURE=str(fx),
               THESIS_ID=thesis_id)
    proc = subprocess.run([node, str(harness)], capture_output=True, env=env,
                          timeout=90)
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 0, "渲染失败:\n%s\n%s" % (err, out[:600])
    assert out, "渲染结果为空"
    return out


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
