"""备源工具 + 「数据缺失」口径收敛 的回归测试（2026-09-21）。

背景（今世缘 run 复盘，docs/diag/2026-09-21-个股报告数据缺失归因.md）：
- 筹码分布 / PE-PB 分位 全项目只有妙想一个来源，妙想额度耗尽后报告整块「数据缺失」；
- 缺失提示在正文/表格/风险矩阵/声明里各写一遍，一份报告里 4 类缺口被写了 20+ 次。

本文件锁死三件事：
1. `stock_analysis.get_chip_distribution` —— AKShare 筹码备源（四字段 + 中文 `fields` 键）；
2. `stock_analysis.get_valuation_percentile` —— PE/PB 历史分位（`pe_percentile` 数据槽的上游）；
3. 缺失口径收敛 —— 板块内只留一句短标注，补齐方式统一进末尾「数据缺口声明」。
"""
import importlib.util
import json
import logging
import threading
from pathlib import Path

import pandas as pd
import pytest


def _load_skill_module():
    """按文件路径加载技能 main.py（tools/skills.py 与 tools/skills/ 同名，不能直接 import）。"""
    path = Path(__file__).resolve().parents[3] / "tools" / "skills" / "stock_analysis" / "main.py"
    spec = importlib.util.spec_from_file_location("stock_analysis_backup_tools", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def skill_cls():
    return _load_skill_module().StockAnalysisSkill


@pytest.fixture()
def skill(skill_cls):
    return skill_cls(logging.getLogger("test-stock-analysis-backup"))


@pytest.fixture(autouse=True)
def _no_cache_writes(monkeypatch):
    """测试不落真实缓存库。"""
    import utils.cache as cache

    for name in ("set_chip_distribution_cache", "set_valuation_history_cache"):
        if hasattr(cache, name):
            monkeypatch.setattr(cache, name, lambda *a, **k: None)
    for name in ("get_chip_distribution_cache", "get_valuation_history_cache"):
        if hasattr(cache, name):
            monkeypatch.setattr(cache, name, lambda *a, **k: None)


# ── 1. 筹码分布备源 ──────────────────────────────────────────

def test_chip_distribution_exposes_four_report_fields(skill, monkeypatch):
    import tools.fetcher as fetcher

    monkeypatch.setattr(fetcher, "ak_chip_distribution", lambda symbol: {
        'code': symbol, 'date': '2026-09-21',
        'profit_ratio': 62.5, 'avg_cost': 28.1,
        'cost_90_low': 25.3, 'cost_90_high': 31.8,
        'concentration_90': 11.4,
        'cost_70_low': 26.4, 'cost_70_high': 30.2, 'concentration_70': 6.8,
    })
    payload = json.loads(skill.get_chip_distribution("603369"))

    # 报告「筹码分布」板块要求的四项必须齐全
    fields = payload["fields"]
    assert fields["获利比例"] == 62.5
    assert fields["平均成本"] == 28.1
    assert fields["90%成本区间"] == [25.3, 31.8]
    assert fields["90%集中度"] == 11.4
    assert payload["source"] == "akshare"
    assert payload["symbol"] == "603369"


def test_chip_distribution_failure_is_structured_not_raised(skill, monkeypatch):
    import tools.fetcher as fetcher

    def _boom(symbol):
        raise RuntimeError("akshare 未获取到筹码分布数据")

    monkeypatch.setattr(fetcher, "ak_chip_distribution", _boom)
    payload = json.loads(skill.get_chip_distribution("603369"))

    assert "error" in payload
    assert payload.get("hint"), "失败时必须给出替代工具提示，否则模型只能写「数据缺失」"


def test_chip_distribution_works_in_worker_thread(skill, monkeypatch):
    """工具在 worker 线程里被调用（Agent 的真实调用位置）。"""
    import tools.fetcher as fetcher

    monkeypatch.setattr(fetcher, "ak_chip_distribution", lambda symbol: {
        'profit_ratio': 50.0, 'avg_cost': 10.0,
        'cost_90_low': 9.0, 'cost_90_high': 11.0, 'concentration_90': 12.0,
    })
    holder = {}

    def worker():
        holder["value"] = skill.get_chip_distribution("603369")

    t = threading.Thread(target=worker, name="asyncio_0")
    t.start()
    t.join(timeout=15)
    assert json.loads(holder["value"])["fields"]["获利比例"] == 50.0


# ── 2. PE/PB 历史分位 ────────────────────────────────────────

def _history_frame(days: int = 300, pe_base: float = 10.0, pb_base: float = 1.0) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=days, freq="D")
    return pd.DataFrame({
        "trade_date": dates,
        "pe_ttm": [pe_base + i * 0.05 for i in range(days)],
        "pb": [pb_base + i * 0.01 for i in range(days)],
    })


def test_valuation_percentile_returns_pe_and_pb(skill, monkeypatch):
    import tools.fetcher as fetcher

    monkeypatch.setattr(fetcher, "ak_valuation_history", lambda symbol, years: _history_frame())
    payload = json.loads(skill.get_valuation_percentile("603369", 5))

    for key in ("pe_percentile", "pb_percentile", "pe_level", "pb_level",
                "pe_ttm", "pb", "years", "history_rows"):
        assert key in payload, f"缺少 {key}"
    assert payload["years"] == 5
    assert 0 <= payload["pe_percentile"] <= 100
    assert payload["pe_ttm"] == pytest.approx(10.0 + 299 * 0.05, abs=0.01)


def test_valuation_percentile_low_end_is_classified_low(skill, monkeypatch):
    """单调上升序列里，末值就是最高分位（≈100%）；反向序列应判为低估。"""
    import tools.fetcher as fetcher

    rising = _history_frame()
    monkeypatch.setattr(fetcher, "ak_valuation_history", lambda symbol, years: rising)
    high = json.loads(skill.get_valuation_percentile("603369", 5))

    falling = rising.copy()
    falling["pe_ttm"] = falling["pe_ttm"].iloc[::-1].values
    falling["pb"] = falling["pb"].iloc[::-1].values
    monkeypatch.setattr(fetcher, "ak_valuation_history", lambda symbol, years: falling)
    low = json.loads(skill.get_valuation_percentile("603369", 5))

    assert high["pe_percentile"] > 90
    assert low["pe_percentile"] < 10
    assert low["pe_level"] in ("低估", "极度低估")


def test_valuation_percentile_failure_is_structured(skill, monkeypatch):
    import tools.fetcher as fetcher

    def _boom(symbol, years):
        raise RuntimeError("akshare 未获取到历史估值数据")

    monkeypatch.setattr(fetcher, "ak_valuation_history", _boom)
    payload = json.loads(skill.get_valuation_percentile("603369", 5))
    assert "error" in payload and payload.get("hint")


# ── 3. fetcher 估值历史取数（百度股市通合并 PE/PB）────────────

class _FakeAk:
    """替身 akshare：只有百度估值接口，没有已下线的 stock_a_indicator_lg。"""

    def __init__(self, rows: int = 400):
        self.rows = rows
        self.calls: list[str] = []

    def stock_zh_valuation_baidu(self, symbol, indicator, period):
        self.calls.append(indicator)
        dates = pd.date_range("2024-01-01", periods=self.rows, freq="D")
        base = 12.0 if "市盈率" in indicator else 1.5
        return pd.DataFrame({"date": dates, "value": [base + i * 0.01 for i in range(self.rows)]})


def _patch_ak(monkeypatch, fake: _FakeAk):
    from tools.fetcher.akshare_ds import AkshareDataSource

    monkeypatch.setattr(AkshareDataSource, "_get_ak", classmethod(lambda cls: fake))
    monkeypatch.setattr(AkshareDataSource, "_enforce_rate_limit",
                        classmethod(lambda cls, **kw: None))
    return AkshareDataSource


def test_valuation_history_merges_pe_and_pb_from_baidu(monkeypatch):
    fake = _FakeAk(rows=400)
    cls = _patch_ak(monkeypatch, fake)

    df = cls.get_valuation_history("603369", years=5)

    assert list(df.columns) == ["trade_date", "pe_ttm", "pb"]
    assert len(df) > 100
    # 升序
    assert df["trade_date"].is_monotonic_increasing
    # 契约：tools/valuation.calc_percentile 依赖 pe_ttm / pb 两列
    assert df["pe_ttm"].notna().all() and df["pb"].notna().all()
    assert set(fake.calls) == {"市盈率(TTM)", "市净率"}


def test_valuation_history_raises_when_no_source_available(monkeypatch):
    class _NoValuationAk:
        def stock_zh_valuation_baidu(self, symbol, indicator, period):  # pragma: no cover
            raise RuntimeError("network down")

    cls = _patch_ak(monkeypatch, _NoValuationAk())
    with pytest.raises(RuntimeError):
        cls.get_valuation_history("603369", years=5)


def test_valuation_history_rejects_hk_us(monkeypatch):
    cls = _patch_ak(monkeypatch, _FakeAk())
    with pytest.raises(RuntimeError):
        cls.get_valuation_history("00700", years=5)  # 港股


# ── 4. 缺失口径收敛 ─────────────────────────────────────────

def test_template_guidance_converges_missing_notes():
    from agents.analysis.template_store import build_guidance, load_template

    text = build_guidance(load_template("stock_deep_dive"), "分析今世缘")
    assert "数据缺口声明" in text, "模板必须要求末尾统一汇总缺口"
    assert "数据缺失：<字段>" in text, "板块内只留一句最短标注"
    # 旧措辞（每处都罗列补齐方式）应已收敛
    assert "如何补齐，禁止省略或整块留空" not in text


def test_report_prompt_converges_missing_notes():
    """report_builder 侧：非必填板块的缺失措辞也必须收敛（用最小模板精准打这条分支）。"""
    from agents.analysis.report_builder import _build_prompt

    template = {
        "role": "测试角色",
        "sections": [{
            "title": "资金面分析",
            "required": False,          # ← 触发「数据不足」措辞分支
            "prompt": "必须引用具体资金数据",
            "max_words": 200,
        }],
    }
    prompt = _build_prompt(template, {}, "分析今世缘")
    text = "\n".join(p if isinstance(p, str) else str(p) for p in prompt) \
        if isinstance(prompt, (list, tuple)) else str(prompt)

    assert "数据缺失：<字段>" in text
    assert "数据缺口声明" in text
    assert "禁止在本板块内重复罗列补齐方案" in text
    assert "如何补齐，禁止省略或整块留空" not in text
