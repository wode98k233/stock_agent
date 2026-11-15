"""factor_report 门禁测试：设计基线与 P0 规则。

这些断言不测「代码跑没跑」，测的是**报告是否符合已冻结的设计契约**：
零 emoji、无外链、中性分层标签、六项口径摘要不缺项。
"""
import re

import pytest

from backtest.factor_eval import EvalConfig, build_panel, evaluate_factors
from backtest.factor_report import build_report_html
from test.unit.backtest.test_factor_eval import _ohlc_frame

# 团队 P0 规则里的 emoji 检测正则（等价子集）
_EMOJI = re.compile(
    "[\U0001F300-\U0001F9FF\u2600-\u26FF\u2700-\u27BF\uFE00-\uFE0F"
    "\U0001F000-\U0001F02F\U0001FA00-\U0001FAFF\u200D\u20E3]"
)


@pytest.fixture(scope="module")
def payload():
    frames = {f"60000{i}": _ohlc_frame(f"60000{i}", n=200, seed=i) for i in range(1, 7)}
    ids = ["rsi_14", "macd_dif", "boll_pctb", "ret_20"]
    panel = build_panel(frames, ids, min_history=60)
    cfg = EvalConfig(ret_period=5, min_names=5, lag=1)
    return evaluate_factors(panel, ids, cfg,
                            meta={"data_range": "2024-01-02 ~ 2026-10-16",
                                  "start": "2024-01-02", "end": "2026-10-16",
                                  "index_code": "000300", "loaded": 6})


@pytest.fixture(scope="module")
def html_out(payload):
    return build_report_html(payload, echarts_js="/* echarts-stub */", detail_top=3)


def test_no_emoji_as_icons(html_out):
    hits = _EMOJI.findall(html_out)
    assert hits == [], f"报告含 emoji 功能图标: {sorted(set(hits))[:5]}"


def test_no_external_resources(html_out):
    assert not re.search(r'(src|href)=["\']https?://', html_out), "存在外链资源（必须可离线）"
    assert "/* echarts-stub */" in html_out, "ECharts 未内联"


def test_neutral_quantile_labels(html_out):
    """分层标签必须中性：不出现「多头层/空头层」（反向因子会误导）。"""
    assert "多头层" not in html_out
    assert "空头层" not in html_out
    assert "Q5−Q1 多空" in html_out
    assert "Q1 = 因子值最小，Q5 = 因子值最大" in html_out


def test_six_appendix_items(html_out):
    for label in ("数据区间", "收益周期", "调仓频率", "复权方式", "中性化处理", "费用假设"):
        assert label in html_out, f"口径摘要缺「{label}」"


def test_ranking_table_columns(html_out):
    for col in ("RankIC", "ICIR(Rank)", "DSR", "校正"):
        assert col in html_out, f"排名表缺列 {col}"
    assert "按 |RankIC| 降序" in html_out


def test_detail_blocks_are_lazy(html_out):
    """详情按需渲染：图表容器在，但只有 toggle 后才 init。"""
    assert html_out.count("<details") == 3
    assert "renderFactor" in html_out
    assert "addEventListener('toggle'" in html_out


def test_payload_embedded_valid_json(html_out):
    import json
    m = re.search(r"window\.__FACTOR_PAYLOAD__ = (\{.*?\});</script>", html_out, re.S)
    assert m, "未嵌入 payload"
    data = json.loads(m.group(1))
    assert data["params"]["ret_period"] == 5
    assert len(data["ranking"]) == 4
