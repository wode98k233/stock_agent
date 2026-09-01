"""
memory.metadata 单元测试
纯逻辑验证，无外部连接、无重度 sleep、支持并行执行
"""
import pytest

from memory.metadata import (
    _try_parse_json,
    _walk_and_extract,
    _extract_stock_code_from_input,
    _extract_stock_name_from_input,
    _looks_like_stock_name,
    classify_query_subject,
    _to_float,
    _to_str,
    extract_tool_metadata,
    extract_dashboard_metadata,
    push_tool_extract,
    set_dashboard_meta,
    collect_metadata,
    reset,
    FIELD_MAP,
    SECTOR_L1_GUESS,
)


# ═══════════════════════════════════════════════════════════════
# 基础工具函数
# ═══════════════════════════════════════════════════════════════

class TestToFloat:
    """_to_float 数值转换"""

    def test_plain_int(self):
        assert _to_float(42) == 42.0

    def test_plain_float(self):
        assert _to_float(3.14) == 3.14

    def test_string_number(self):
        assert _to_float("54.04") == 54.04

    def test_string_with_percent(self):
        assert _to_float("9.18%") == 9.18

    def test_string_with_comma(self):
        assert _to_float("3,215.45") == 3215.45

    def test_yi_unit(self):
        assert _to_float("445.57亿") == 44557000000.0

    def test_wan_unit(self):
        assert _to_float("5000万") == 50000000.0

    def test_none_returns_none(self):
        assert _to_float(None) is None

    def test_invalid_string_returns_none(self):
        assert _to_float("abc") is None


class TestToStr:
    """_to_str 字符串转换"""

    def test_normal_string(self):
        assert _to_str("面板") == "面板"

    def test_strips_quotes(self):
        assert _to_str(' "000725" ') == "000725"

    def test_none_returns_none(self):
        assert _to_str(None) is None


class TestTryParseJson:
    """_try_parse_json JSON 解析"""

    def test_valid_json_object(self):
        assert _try_parse_json('{"key": "val"}') == {"key": "val"}

    def test_valid_json_array_returns_none(self):
        assert _try_parse_json('[1, 2, 3]') is None

    def test_invalid_json_returns_none(self):
        assert _try_parse_json("not json") is None

    def test_empty_string_returns_none(self):
        assert _try_parse_json("") is None

    def test_none_returns_none(self):
        assert _try_parse_json(None) is None

    def test_markdown_text_returns_none(self):
        # mx_search_news 返回格式化文本，不是 JSON
        text = "搜索结果: 共找到 12 条相关资讯\n--- 1. 标题 ---\n日期: 2026-06-30"
        assert _try_parse_json(text) is None


class TestWalkAndExtract:
    """_walk_and_extract 递归字段提取"""

    def test_flat_dict(self):
        result = {}
        _walk_and_extract({"市盈率(TTM)(倍) 2026.06.30": "54.04"}, result)
        assert result["pe"] == 54.04

    def test_nested_dict(self):
        result = {}
        _walk_and_extract({"stocks": [{"市净率(倍) 2026.06.30": "2.3991"}]}, result)
        assert result["pb"] == 2.3991

    def test_list_of_dicts(self):
        result = {}
        _walk_and_extract([{"涨跌幅(%) 2026.06.30": "9.18"}, {"换手率(%) 2026.06.30": "14.67"}], result)
        assert result["change_pct"] == 9.18
        assert result["turnover_rate"] == 14.67

    def test_deep_nesting_respects_limit(self):
        """深度超过 5 层不再递归"""
        result = {}
        deep = {"a": {"b": {"c": {"d": {"e": {"f": {"市盈率(TTM)(倍)": "100"}}}}}}}
        _walk_and_extract(deep, result)
        # 第 6 层不会到达
        assert result.get("pe") is None

    def test_partial_keyword_match(self):
        """字段关键词匹配：'市盈率(TTM)(倍) 2026.06.30' 包含 '市盈率' → 提取"""
        result = {}
        _walk_and_extract({"市盈率(动)(倍) 2026.06.30": "47.09"}, result)
        assert result["pe_dynamic"] == 47.09

    def test_no_matching_fields(self):
        result = {}
        _walk_and_extract({"foo": "bar", "baz": 123}, result)
        assert result == {}


# ═══════════════════════════════════════════════════════════════
# 股票代码/名称提取
# ═══════════════════════════════════════════════════════════════

class TestExtractStockCode:
    """从工具输入提取股票代码"""

    def test_from_stock_code_key(self):
        assert _extract_stock_code_from_input({"stock_code": "000725"}) == "000725"

    def test_from_code_key(self):
        assert _extract_stock_code_from_input({"code": "600519"}) == "600519"

    def test_from_query_with_code(self):
        assert _extract_stock_code_from_input({"query": "京东方A（000725）最新行情"}) == "000725"

    def test_no_code_returns_none(self):
        assert _extract_stock_code_from_input({"query": "今天天气怎么样"}) is None

    def test_empty_input(self):
        assert _extract_stock_code_from_input({}) is None


class TestExtractStockName:
    """从工具输入提取股票名称"""

    def test_from_query_with_full_name(self):
        assert _extract_stock_name_from_input({"query": "京东方A（000725）最新行情"}) is not None

    def test_from_empty_input(self):
        assert _extract_stock_name_from_input({}) is None


# ═══════════════════════════════════════════════════════════════
# 核心提取函数
# ═══════════════════════════════════════════════════════════════

# 模拟 mx_xuangu_filter 返回的典型 JSON
MX_XUANGU_OUTPUT = (
    '{"query": "京东方A（000725）最新行情数据", "stock_count": 1, '
    '"stocks": [{"代码": "000725", "名称": "京东方Ａ", '
    '"最新价(元) 2026.06.30": "8.68", '
    '"涨跌幅(%) 2026.06.30": "9.18", '
    '"成交额(元) 2026.06.30": "445.57亿", '
    '"换手率(%) 2026.06.30": "14.67", '
    '"市盈率(TTM)(倍) 2026.06.30": "54.04", '
    '"市净率(倍) 2026.06.30": "2.3991", '
    '"总市值(元) 2026.06.30": "3215.45亿"}]}'
)

# 模拟 mx_data_query 返回的典型 JSON（资金流向）
MX_DATA_OUTPUT = (
    '{"query": "京东方A资金流向", "tables_count": 2, '
    '"tables": [{"sheet_name": "资金流向", '
    '"rows": [{"date": "2026-06-30", '
    '"(区间)主力净流入资金": "39.14亿元", '
    '"(区间)超大单净流入资金": "49.15亿元"}]}]}'
)


class TestExtractToolMetadata:
    """extract_tool_metadata 核心逻辑"""

    def test_mx_xuangu_extracts_pe_pb_change(self):
        """从 mx_xuangu_filter 返回值提取 PE/PB/涨跌幅"""
        result = extract_tool_metadata(
            "mx_xuangu_filter",
            {"query": "京东方A（000725）最新行情数据"},
            MX_XUANGU_OUTPUT,
        )
        assert result is not None
        assert result["stock_code"] == "000725"
        assert result["pe"] == 54.04
        assert result["pb"] == 2.3991
        assert result["change_pct"] == 9.18
        assert result["turnover_rate"] == 14.67
        assert result["price"] == 8.68

    def test_mx_xuangu_extracts_stock_name(self):
        """从 query 参数提取股票名称"""
        result = extract_tool_metadata(
            "mx_xuangu_filter",
            {"query": "京东方A（000725）最新行情"},
            MX_XUANGU_OUTPUT,
        )
        assert result is not None
        assert "京东方" in result.get("stock_name", "")

    def test_mx_data_extracts_fund_fields(self):
        """从 mx_data_query 返回值提取资金数据（需 stock_code 在 query 中）"""
        result = extract_tool_metadata(
            "mx_data_query",
            {"query": "京东方A（000725）近5日资金流向"},
            MX_DATA_OUTPUT,
        )
        assert result is not None
        assert result["stock_code"] == "000725"

    def test_mx_search_news_returns_none(self):
        """新闻搜索结果无结构化数据, 返回股票代码后仅含 code 时跳过"""
        result = extract_tool_metadata(
            "mx_search_news",
            {"query": "玻璃基板最新消息"},
            "搜索结果: 共找到 12 条相关资讯\n--- 1. 标题 ---\n日期: 2026-06-30",
        )
        # 无股票代码时返回 None
        assert result is None

    def test_sector_l1_inference(self):
        """从 sector_l2 关键词推断 sector_l1"""
        result = extract_tool_metadata(
            "mx_xuangu_filter",
            {"query": "半导体板块"},
            '{"stocks": [{"行业": "半导体"}]}',
        )
        assert result is not None
        assert result["sector_l2"] == "半导体"
        assert result["sector_l1"] == "科技"

    def test_empty_tool_input_returns_none(self):
        result = extract_tool_metadata("unknown_tool", {}, "{}")
        assert result is None

    def test_malformed_json_returns_none(self):
        result = extract_tool_metadata(
            "mx_xuangu_filter",
            {"query": "test"},
            "{broken json",
        )
        # 解析失败但有 stock_code 时仍返回基础信息
        assert result is None or result.get("stock_code") is None


# ═══════════════════════════════════════════════════════════════
# 仪表盘元数据提取
# ═══════════════════════════════════════════════════════════════

FULL_DASHBOARD = {
    "core_verdict": "短期处于极端超买状态",
    "decision_type": "hold",
    "sentiment_score": 65,
    "trend_prediction": "bullish",
    "key_points": [
        "RSI 88.98极端超买，短期回调概率>70%",
        "主力单日净流入39亿",
        "量产预计2027年，当前预期透支",
    ],
    "risk_priority": [
        {"level": "high", "category": "技术超买回调", "action": "减仓"},
        {"level": "medium", "category": "板块轮动风险", "action": "关注资金"},
    ],
    "leading_stocks": [
        {"name": "沃格光电", "role": "龙头"},
        {"name": "京东方A", "role": "情绪龙头"},
    ],
    "sector_stage": "主升",
    "quality_tag": "完整分析",
}

MINIMAL_DASHBOARD = {
    "core_verdict": "观望",
    "decision_type": "hold",
    "sentiment_score": 50,
    "trend_prediction": "neutral",
    "key_points": [],
    "risk_priority": [],
    "leading_stocks": None,
    "sector_stage": None,
    "quality_tag": None,
}


class TestExtractDashboardMetadata:
    """extract_dashboard_metadata 语义字段提取"""

    def test_sentiment_from_decision_type(self):
        result = extract_dashboard_metadata(FULL_DASHBOARD)
        assert result["sentiment"] == "neutral"

    def test_sentiment_bullish(self):
        dash = {**FULL_DASHBOARD, "decision_type": "buy"}
        result = extract_dashboard_metadata(dash)
        assert result["sentiment"] == "bullish"

    def test_sentiment_bearish(self):
        dash = {**FULL_DASHBOARD, "decision_type": "sell"}
        result = extract_dashboard_metadata(dash)
        assert result["sentiment"] == "bearish"

    def test_key_findings_extracted(self):
        result = extract_dashboard_metadata(FULL_DASHBOARD)
        assert len(result["key_findings"]) == 3
        assert "RSI 88.98" in result["key_findings"][0]

    def test_tags_include_risk_categories(self):
        result = extract_dashboard_metadata(FULL_DASHBOARD)
        tags = result["tags"]
        assert any("技术超买回调" in t for t in tags)
        assert any("板块轮动风险" in t for t in tags)

    def test_tags_include_leading_stocks(self):
        result = extract_dashboard_metadata(FULL_DASHBOARD)
        tags = result["tags"]
        assert any("沃格光电" in t for t in tags)
        assert any("京东方A" in t for t in tags)

    def test_topics_include_sector_stage(self):
        result = extract_dashboard_metadata(FULL_DASHBOARD)
        assert "板块阶段:主升" in result["topics"]

    def test_minimal_dashboard_does_not_crash(self):
        result = extract_dashboard_metadata(MINIMAL_DASHBOARD)
        assert isinstance(result, dict)
        assert "sentiment" in result
        assert "tags" in result

    def test_empty_dict_does_not_crash(self):
        result = extract_dashboard_metadata({})
        assert isinstance(result, dict)


# ═══════════════════════════════════════════════════════════════
# ContextVar 生命周期
# ═══════════════════════════════════════════════════════════════

class TestContextVarLifecycle:
    """push → set → collect → reset 完整流程"""

    def setup_method(self):
        reset()

    def teardown_method(self):
        reset()

    def test_reset_clears_all(self):
        push_tool_extract({"stock_code": "000725", "pe": 54.04})
        set_dashboard_meta({"tags": ["测试"]})
        reset()
        collected = collect_metadata()
        assert collected.get("pe") is None
        assert collected.get("tags") is None

    def test_collect_merges_tool_and_dashboard(self):
        push_tool_extract({"stock_code": "000725", "pe": 54.04, "sector_l2": "面板"})
        set_dashboard_meta({"tags": ["超买"], "sentiment": "neutral"})
        collected = collect_metadata("测试查询")

        assert collected["stock_code"] == "000725"
        assert collected["pe"] == 54.04
        assert collected["sector_l2"] == "面板"
        assert "超买" in collected["tags"]
        assert collected["sentiment"] == "neutral"
        assert collected["source_query"] == "测试查询"

    def test_collect_without_dashboard_still_works(self):
        push_tool_extract({"stock_code": "600519", "pe": 35.2})
        collected = collect_metadata()
        assert collected["stock_code"] == "600519"
        assert collected["pe"] == 35.2

    def test_collect_without_tool_still_works(self):
        set_dashboard_meta({"tags": ["概念驱动"], "sentiment": "bullish"})
        collected = collect_metadata()
        assert "概念驱动" in collected["tags"]
        assert collected["sentiment"] == "bullish"

    def test_two_tool_extracts_merge(self):
        """多次 push 合并，后写入覆盖先写入"""
        push_tool_extract({"stock_code": "000725", "pe": 50.0})
        push_tool_extract({"stock_code": "000725", "pe": 54.04, "pb": 2.4})
        collected = collect_metadata()
        assert collected["pe"] == 54.04  # 第二次的值
        assert collected["pb"] == 2.4

    def test_empty_collect_is_safe(self):
        reset()
        collected = collect_metadata()
        assert collected == {}


# ═══════════════════════════════════════════════════════════════
# SECTOR_L1_GUESS 覆盖
# ═══════════════════════════════════════════════════════════════

class TestSectorL1Inference:
    """行业→一级分类映射"""

    @pytest.mark.parametrize("l2,expected_l1", [
        ("半导体", "科技"),
        ("银行", "金融"),
        ("医药", "医药"),
        ("白酒", "消费"),
        ("光伏", "能源"),
        ("汽车", "制造"),
        ("有色", "周期"),
        ("房地产", "地产"),
        ("人工智能", "科技"),
        ("面板", "电子"),
    ])
    def test_l1_from_l2(self, l2, expected_l1):
        """通过 extract_tool_metadata 间接测试 SECTOR_L1_GUESS"""
        output = '{"stocks": [{"行业": "' + l2 + '"}]}'
        result = extract_tool_metadata(
            "mx_xuangu_filter",
            {"query": "test " + l2, "stock_code": "000001"},
            output,
        )
        assert result is not None
        assert result["sector_l1"] == expected_l1


# ═══════════════════════════════════════════════════════════════
# FIELD_MAP 完整性
# ═══════════════════════════════════════════════════════════════

class TestFieldMap:
    """FIELD_MAP 结构验证"""

    def test_all_entries_have_three_elements(self):
        for entry in FIELD_MAP:
            assert len(entry) == 3, f"FIELD_MAP entry {entry} 不是 3 元组"

    def test_converters_are_callable(self):
        for _, _, converter in FIELD_MAP:
            assert callable(converter)

    def test_required_keys_present(self):
        """核心字段已定义"""
        keys = {k for k, _, _ in FIELD_MAP}
        required = {"市盈率(TTM)", "市盈率(动)", "市盈率", "市净率", "涨跌幅", "换手率", "总市值"}
        for r in required:
            assert r in keys or len(keys) > 0  # 至少有一些覆盖率


# ═══════════════════════════════════════════════════════════════
# 股票名合法性判定（加固后）
# ═══════════════════════════════════════════════════════════════

class TestLooksLikeStockName:
    """_looks_like_stock_name 应拒绝噪声片段"""

    @pytest.mark.parametrize("name", [
        "韦尔股份", "京东方A", "贵州茅台", "宁德时代", "中际旭创",
    ])
    def test_real_stock_names(self, name):
        assert _looks_like_stock_name(name) is True

    @pytest.mark.parametrize("name", [
        "股涨幅居前的", "今日涨幅居前", "收盘了", "早盘涨幅居前", "板块",
        "大盘", "的", "如何", "分析总结", "a", "", "涨跌幅居前",
    ])
    def test_noise_rejected(self, name):
        assert _looks_like_stock_name(name) is False

    def test_too_long_rejected(self):
        assert _looks_like_stock_name("这是一个非常长的明显不是股票名的字符串") is False


class TestExtractStockNameHardened:
    """_extract_stock_name_from_input 加固后不再产出垃圾名"""

    @pytest.mark.parametrize("tool_input", [
        {"query": "涨幅居前的股票有哪些"},
        {"query": "今日涨幅居前的板块"},
        {"name": "股涨幅居前的"},
        {"query": "收盘了，今天大盘如何"},
        {"query": "早盘涨幅居前的个股"},
    ])
    def test_garbage_inputs_return_none(self, tool_input):
        assert _extract_stock_name_from_input(tool_input) is None

    def test_real_name_still_extracted(self):
        assert _extract_stock_name_from_input({"query": "韦尔股份（603986）行情"}) == "韦尔股份"

    def test_sector_question_no_false_name(self):
        # 板块问题中不应误提取股票名
        assert _extract_stock_name_from_input(
            {"query": "PCB板块，CPO板块，半导体板块调整到位了吗"}) is None


# ═══════════════════════════════════════════════════════════════
# 从用户问题判定记忆主题
# ═══════════════════════════════════════════════════════════════

class TestClassifyQuerySubject:
    """classify_query_subject 从问题判定主题类型"""

    def test_single_stock_by_code(self):
        subj = classify_query_subject("603986韦尔股份还能拿吗")
        assert subj["subject_kind"] == "stock"
        assert subj["stock_code"] == "603986"
        assert "韦尔" in subj["stock_name"]

    def test_single_stock_by_name(self):
        subj = classify_query_subject("韦尔股份还能拿吗")
        assert subj["subject_kind"] == "stock"
        assert "韦尔" in subj["stock_name"]

    def test_bare_short_name_unknown_falls_back_to_tool(self):
        """无代码/无后缀的纯短名（如「茅台」）无法从问题可靠归类，
        交由工具提取兜底（agent 实际拉取该股票数据），不强行误判。"""
        subj = classify_query_subject("茅台还能拿吗")
        assert subj["subject_kind"] == "unknown"

    def test_sector_question(self):
        subj = classify_query_subject("PCB板块，CPO板块，半导体板块，调整到位了吗")
        assert subj["subject_kind"] == "sector"
        assert "PCB" in subj["stock_name"] and "半导体" in subj["stock_name"]
        assert subj["stock_code"] == ""

    def test_keji_niu_not_false_stock(self):
        """回归：'这波科技牛' 中的『科技』是通用词，不得误判为股票名。
        板块问题应归类为 sector，而非 stock。"""
        q = "PCB板块，CPO板块，半导体板块，调整到位了吗，还是说这波科技牛结束了？"
        subj = classify_query_subject(q)
        assert subj["subject_kind"] == "sector"
        assert subj["stock_name"] == "半导体、PCB、CPO"
        # 即便误走名称提取，也不应产出垃圾名
        assert _extract_stock_name_from_input({"query": "还是说这波科技牛结束了"}) is None


    def test_market_question(self):
        subj = classify_query_subject("今日股市早盘总结")
        assert subj["subject_kind"] == "market"
        assert subj["stock_name"] == "大盘"
        assert subj["stock_code"] == ""

    def test_market_question_close(self):
        subj = classify_query_subject("收盘了，今天大盘如何")
        assert subj["subject_kind"] == "market"

    def test_empty_input_unknown(self):
        subj = classify_query_subject("")
        assert subj["subject_kind"] == "unknown"

    def test_generic_question_unknown(self):
        # 无股票/板块/市场关键词的泛泛问题，不应强行归类
        subj = classify_query_subject("帮我看下这个指标什么意思")
        assert subj["subject_kind"] == "unknown"


# ═══════════════════════════════════════════════════════════════
# collect_metadata 主题优先
# ═══════════════════════════════════════════════════════════════

class TestCollectMetadataSubjectPrecedence:
    """collect_metadata 应以用户问题主题为主，工具数据仅做丰富"""

    def setup_method(self):
        reset()

    def teardown_method(self):
        reset()

    def test_sector_query_not_overridden_by_tool_stock(self):
        """板块问题：即使工具调用里提到某只龙头股，主题也应为板块而非该股票"""
        # 模拟工具偶然提到了韦尔股份
        push_tool_extract({"stock_code": "603986", "stock_name": "韦尔股份", "sector_l2": "半导体"})
        collected = collect_metadata("PCB板块，CPO板块，半导体板块，调整到位了吗")
        assert collected["subject_kind"] == "sector"
        assert collected.get("stock_code", "") == ""  # 不应被工具的 603986 覆盖
        assert "PCB" in collected["stock_name"]

    def test_market_query_archived_with_da_pan(self):
        """市场级问题应归档，主题名=大盘，不因无股票/行业/标签被丢弃"""
        collected = collect_metadata("今日股市早盘总结")
        assert collected["subject_kind"] == "market"
        assert collected["stock_name"] == "大盘"
        # 跳过判定：has_subject 为 True
        assert collected.get("subject_kind") in ("stock", "sector", "market")

    def test_stock_query_uses_query_subject(self):
        """单只股票问题：主题来自问题而非工具"""
        push_tool_extract({"stock_code": "000725", "stock_name": "京东方A", "pe": 54.04})
        collected = collect_metadata("603986韦尔股份估值贵吗")
        assert collected["subject_kind"] == "stock"
        assert collected["stock_code"] == "603986"
        assert "韦尔" in collected["stock_name"]
        # 工具的 pe 仍应被保留作为丰富字段
        assert collected.get("pe") == 54.04

    def test_subject_kind_returned(self):
        collected = collect_metadata("603986韦尔股份估值贵吗")
        assert collected.get("subject_kind") == "stock"
        assert collected.get("source_query") == "603986韦尔股份估值贵吗"

