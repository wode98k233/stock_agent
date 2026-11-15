"""因子先验（S4 F5-A）单测。

关键契约：
- 没先验文件时 load_prior 返回 None，prior_prompt_text 返回空串（**不编造**）
- 先验文本有长度上限（喂给 LLM，不能变成整张因子表）
- 想法点名「脆弱/反向」因子 → 产生提醒，但**不阻断**（runner 层只是文案附加）
- 跨池比对必须支持「符号相反 → fragile」（这是本项目 ADX 的真实结论）
"""
import json
from pathlib import Path

import pytest

from backtest import factor_prior as fp


def _report(pool, top, dsr_pass, ic=None):
    return {
        "report_uuid": f"FR-{pool}", "index_code": pool, "title": "t",
        "params": {"ret_period": 5}, "kpi": {"top_factor": top, "dsr_pass": dsr_pass},
        "created_at": "2026-10-17 12:00:00",
    }


def _compare(rows, pools=("000300", "000905")):
    return {"compare": {"pools": list(pools), "rankic_spearman": 0.88,
                        "sign_agree_ratio": 0.9, "rows": rows},
            "meta": {}}


# ── 汇总逻辑 ────────────────────────────────────────────

def test_robust_requires_both_pools_dsr_and_same_sign():
    rows = [{"factor_id": "boll_pctb", "rankic_a": 0.03, "rankic_b": 0.028,
             "sign_agree": True, "dsr_a": 0.99, "dsr_b": 0.97},
            {"factor_id": "kdj_k", "rankic_a": 0.03, "rankic_b": 0.028,
             "sign_agree": True, "dsr_a": 0.99, "dsr_b": 0.4},
            {"factor_id": "dmi_adx", "rankic_a": 0.02, "rankic_b": -0.015,
             "sign_agree": False, "dsr_a": 0.2, "dsr_b": 0.3}]
    prior = fp.build_prior(reports=[_report("000300", "x", 0), _report("000905", "x", 0)],
                           compare=_compare(rows))
    f = prior["factors"]
    assert f["boll_pctb"]["robust"] is True
    assert f["kdj_k"]["robust"] is False        # 只一池过校正不算稳健
    assert f["dmi_adx"]["fragile"] is True      # 跨池变号 → 脆弱
    assert "boll_pctb" in prior["summary"]["robust"]
    assert "dmi_adx" in prior["summary"]["fragile"]


def test_direction_and_reverse_summary():
    rows = [{"factor_id": "ma_bias_20", "rankic_a": -0.03, "rankic_b": -0.028,
             "sign_agree": True, "dsr_a": 0.2, "dsr_b": 0.2}]
    prior = fp.build_prior(reports=[_report("000300", "x", 0)], compare=_compare(rows))
    assert prior["factors"]["ma_bias_20"]["direction"] == -1
    assert "ma_bias_20" in prior["summary"]["reverse"]


def test_single_pool_marks_no_cross_pool():
    prior = fp.build_prior(reports=[_report("000300", "x", 0)], compare=None)
    assert prior["source"]["cross_pool"] is False
    assert prior["summary"]["robust"] == []       # 无跨池数据不给"稳健"结论


def test_no_reports_no_crash():
    prior = fp.build_prior(reports=[], compare=None)
    assert prior["summary"]["n"] == 36           # 因子注册表全量
    assert prior["factors"]["dmi_adx"]["name"]


# ── 提示文本 ────────────────────────────────────────────

def test_prompt_text_bounded_length():
    ids = ["ret_60", "ret_20", "ret_5", "ret_10", "ma_bias_60", "ma_bias_10",
           "ma_bias_5", "ema_bias_12", "macd_dif", "macd_dea", "macd_hist",
           "boll_pctb", "boll_bandwidth", "rsi_24", "rsi_14", "rsi_6", "kdj_k",
           "kdj_d", "kdj_j", "wr_14", "cci_20", "psy_12", "atr_pct",
           "volatility_20", "volatility_60", "close_position_20",
           "price_range_20", "dmi_adx", "dmi_di_diff", "vol_ratio_5_20",
           "amount_ratio_5", "obv_slope_20", "vr_26", "ma_spread_5_20",
           "ma_spread_20_60"]
    rows = [{"factor_id": fid, "rankic_a": -0.02 - i * 0.0005, "rankic_b": -0.02,
             "sign_agree": True, "dsr_a": 0.1, "dsr_b": 0.1}
            for i, fid in enumerate(ids)]
    prior = fp.build_prior(reports=[_report("000300", "x", 0)], compare=_compare(rows))
    text = fp.prior_prompt_text(prior)
    assert text and len(text) < 600              # 注入 LLM 的文本有上限
    assert "方向为负" in text


def test_prompt_text_empty_without_prior():
    assert fp.prior_prompt_text({"factors": {}, "summary": {}}) == ""


def test_warnings_trigger_on_fragile_and_reverse(tmp_path, monkeypatch):
    prior = {
        "factors": {
            "dmi_adx": {"name": "ADX(14)", "fragile": True, "direction": 1},
            "macd_dif": {"name": "MACD-DIF", "fragile": False, "direction": -1},
        },
        "summary": {"fragile": ["dmi_adx"], "reverse": ["macd_dif"]},
        "source": {"cross_pool": True},
    }
    warns = fp.prior_warnings("用 ADX 和 MACD 做择时", prior)
    assert len(warns) == 2
    assert any("ADX" in w for w in warns)
    assert any("MACD" in w for w in warns)
    assert not fp.prior_warnings("随便买点", prior)   # 没点名就不提醒


def test_load_prior_missing_returns_none(tmp_path):
    assert fp.load_prior(tmp_path / "nope.json") is None


def test_load_prior_corrupt_returns_none(tmp_path):
    bad = tmp_path / "p.json"
    bad.write_text("{not json", encoding="utf-8")
    assert fp.load_prior(bad) is None


def test_load_prior_roundtrip(tmp_path, monkeypatch):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"factors": {"x": {"name": "X"}}, "summary": {}}),
                 encoding="utf-8")
    assert fp.load_prior(p)["factors"]["x"]["name"] == "X"


# ── 注入点接线 ──────────────────────────────────────────

def test_strategy_prompt_injection_point(tmp_path, monkeypatch):
    """策略研究的 prompt 注入点必须真的带上先验（_prior_note 薄封装）。"""
    p = tmp_path / "factor_prior.json"
    p.write_text(json.dumps({
        "factors": {"dmi_adx": {"name": "ADX(14)"}},
        "summary": {"robust": ["dmi_adx"], "fragile": [], "reverse": []},
        "source": {"cross_pool": True},
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(fp, "PRIOR_PATH", p)
    monkeypatch.setattr(fp, "_CACHE", {"mtime": None, "data": None})

    from backtest import strategy_research
    note = strategy_research.StrategyGenerator._prior_note(object())
    assert "ADX" in note and "稳健" in note
    assert note.startswith("\n") and note.endswith("\n"), "注入需带换行，避免黏住上一行"


def test_prior_note_empty_without_prior_file(tmp_path, monkeypatch):
    monkeypatch.setattr(fp, "PRIOR_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(fp, "_CACHE", {"mtime": None, "data": None})
    from backtest import strategy_research
    assert strategy_research.StrategyGenerator._prior_note(object()) == ""


def test_confirm_text_warning_wiring():
    """strategy_gate.build_confirm_text 会附加 prior_warnings（源码级断言）。"""
    src = Path("agents/shared/strategy_gate.py").read_text(encoding="utf-8")
    assert "prior_warnings" in src
