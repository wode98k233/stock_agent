"""外部技能联网冒烟（默认跳过；消耗妙想 / 同花顺每日配额，务必谨慎）。

配额纪律
--------
- 妙想（mx_*）与同花顺（iwc_*）共用每日约 100 次的调用额度，且实测会出现
  「状态码 122 / 数据量已达到上限」。
- 因此本模块**默认整体跳过**，只有显式设置 `SR_LIVE_SKILLS=1` 才运行。
- 运行时有**硬预算闸门**（默认 6 次，可用 `SR_LIVE_SKILLS_BUDGET` 覆盖），
  每发起一次外部调用前计数，超预算直接失败——宁可测试红，不可把配额打光。

启用方式
--------
    SR_LIVE_SKILLS=1 python -m pytest test/integration/test_external_skills_live.py -v
    # 只跑 mx_data 相关（省配额）
    SR_LIVE_SKILLS=1 python -m pytest test/integration/test_external_skills_live.py -v -k mx_data

断言口径
--------
只断言**结构**与**不崩**，不断言具体数值（行情每天在变，断言数值必然误报）。

配额耗尽 ≠ 测试失败
-------------------
实测 2026-09-20：妙想返回「状态码 122 - 您本周的妙想金融数据skill请求的数据量已达到上限」。
此情况下正常路径无法验证，但**恰好复现了 P0-1 的真实触发条件**（额度耗尽时妙想返回
`"data": null`，修复前必然抛 `NoneType.get`）。因此配额/限流命中时一律 `skip`，
只有真实缺陷才 `fail`。
"""
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

if os.getenv("SR_LIVE_SKILLS") != "1":
    pytest.skip(
        "联网冒烟默认跳过；设 SR_LIVE_SKILLS=1 启用（会消耗妙想/同花顺每日约 100 次配额）",
        allow_module_level=True,
    )

pytestmark = [
    pytest.mark.integration,
    pytest.mark.mx,
    pytest.mark.timeout(90),
]

# P0-1 修复的指纹：修复前该字符串会出现在返回体里（解析层把 None 当 dict 用）
_NONE_TYPE_FINGERPRINT = "NoneType' object has no attribute 'get'"

# 配额/限流：属于"环境不可用"，不是被测代码的问题 → 应当 skip 而非 fail。
# 实测 2026-09-20 妙想返回：状态码 122 - 您本周的妙想金融数据skill请求的数据量已达到上限
_QUOTA_MARKERS = (
    "状态码 122",
    "达到上限",
    "操作过于频繁",
    "请求频率过高",
    "Too many requests",
)


def _is_quota_blocked(payload) -> bool:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return any(marker in text for marker in _QUOTA_MARKERS)


def _skip_or_fail_on_failure(out, label: str) -> None:
    """失败时区分「配额耗尽」与「真实缺陷」：前者 skip，后者 fail。"""
    error = out.get("error") if isinstance(out, dict) else None
    if _is_quota_blocked(out):
        pytest.skip(f"{label}：妙想/同花顺配额或限流不可用，无法验证 → {error}")
    pytest.fail(f"{label} 失败: {error}")

_LIVE_BUDGET = int(os.getenv("SR_LIVE_SKILLS_BUDGET", "6"))
_spent = {"n": 0}


@pytest.fixture(scope="module")
def live():
    """外部调用闸门：每次调用前计数，超预算直接 fail（而不是静默花光配额）。"""
    def _spend(label: str) -> None:
        _spent["n"] += 1
        if _spent["n"] > _LIVE_BUDGET:
            pytest.fail(
                f"外部调用超预算（{_LIVE_BUDGET} 次，本次为 {label}）；"
                "如需继续请显式提高 SR_LIVE_SKILLS_BUDGET"
            )
    yield _spend
    print(f"\n[live] 本次共发起 {_spent['n']} 次外部调用（预算 {_LIVE_BUDGET}）")


@pytest.fixture(scope="module")
def register():
    from tools.skill_register import SkillRegister

    reg = SkillRegister()
    reg.auto_discover(fast=False)
    return reg


def _invoke(register, skill: str, params: dict):
    """按运行期真实路径调用技能工具（含参数 schema 校验），返回解析后的对象。"""
    tools = register.build_langchain_tools(skill, __import__("logging").getLogger("live"), None)
    assert tools, f"技能 {skill} 未产出任何工具"
    raw = tools[0].invoke(params)
    return json.loads(raw) if isinstance(raw, str) else raw


# ── P0-1 回归：空结果不得再炸链式 .get ─────────────────────


def test_mx_data_empty_result_degrades_cleanly(register, live):
    """难以命中的查询：返回空结果或可读错误，绝不能出现 NoneType 解析崩溃。

    这是 2026-09-20 修复（tools/other_skills/eastmoney/mx_common.py::as_dict）的
    联网回归验证——修复前该查询会命中「妙想返回 data=None」并抛
    'NoneType' object has no attribute 'get'。
    """
    live("mx_data_query(空结果回归)")
    out = _invoke(register, "mx_data", {"query": "代码999999999999的每股经营现金流TTM"})

    assert isinstance(out, dict)
    assert _NONE_TYPE_FINGERPRINT not in json.dumps(out, ensure_ascii=False)
    # 要么有表，要么给出可读的失败原因；不允许静默空 dict
    assert "tables_count" in out or out.get("status") == "failed"
    if out.get("status") == "failed":
        assert out.get("error"), "失败时必须给出可读错误原因"


def test_mx_data_normal_query_returns_tables(register, live):
    live("mx_data_query(正常查询)")
    out = _invoke(register, "mx_data", {"query": "贵州茅台最新价 涨跌幅"})

    assert isinstance(out, dict)
    assert _NONE_TYPE_FINGERPRINT not in json.dumps(out, ensure_ascii=False)
    if out.get("status") == "failed":
        _skip_or_fail_on_failure(out, "mx_data_query(正常查询)")
    assert out.get("tables_count", 0) >= 1
    assert out.get("terminal_output")


# ── 其余外部技能：只验结构 ──────────────────────────────────


def test_mx_search_news_returns_result(register, live):
    live("mx_search_news")
    out = _invoke(register, "mx_search", {"query": "A股 今日 收盘 复盘"})

    assert isinstance(out, dict)
    if _is_quota_blocked(out):
        pytest.skip(f"妙想配额/限流不可用 → {out.get('error') or out}")
    assert isinstance(out.get("result"), str)
    assert out["result"].strip()


def test_iwc_query_returns_something(register, live):
    live("iwc_query")
    out = _invoke(register, "hithink_market_query", {"query": "贵州茅台 最新价 涨跌幅"})

    assert out is not None
    text = json.dumps(out, ensure_ascii=False)
    assert _NONE_TYPE_FINGERPRINT not in text
    if _is_quota_blocked(out):
        pytest.skip(f"同花顺配额/限流不可用 → {text[:160]}")
    assert len(text) > 2


def test_mx_xuangu_filter_returns_shape(register, live):
    live("mx_xuangu_filter")
    out = _invoke(register, "mx_xuangu", {"query": "银行板块 股息率大于5% 的股票"})

    assert out is not None
    text = json.dumps(out, ensure_ascii=False)
    assert _NONE_TYPE_FINGERPRINT not in text
    if _is_quota_blocked(out):
        pytest.skip(f"妙想配额/限流不可用 → {text[:160]}")
    assert len(text) > 2
