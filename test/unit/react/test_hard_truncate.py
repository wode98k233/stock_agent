"""工具输出硬截断 + 压缩 prompt 三类引导的回归测试。

背景（2026-09-21 实测）：`TOOL_HARD_TRUNCATE_CHARS=8000` 的纯头截断
（`content[:limit]`）把按**时间升序**的 `get_etf_history` 输出（260 行日K，
2025-08-27~2026-09-21）砍成只剩最早一段，报告据此误报「日K未覆盖当日序列」。
改为保留两头（头部字段名 + 尾部最近数据）后，无论升/降序都能保住最近一端。
"""
import json

import pytest

from agents.react.utils import _COMPRESS_SYSTEM_PROMPT, _hard_truncate_keep_both_ends

pytestmark = pytest.mark.unit


class TestHardTruncateBothEnds:
    def test_short_content_untouched(self):
        s = "abc" * 10
        assert _hard_truncate_keep_both_ends(s, 8000) == s

    def test_zero_limit_untouched(self):
        s = "x" * 100
        assert _hard_truncate_keep_both_ends(s, 0) == s

    def test_keeps_head_and_tail_and_marks_omission(self):
        content = "H" * 500 + "M" * 5000 + "T" * 500
        out = _hard_truncate_keep_both_ends(content, 1000)
        assert len(out) <= 1000 + 200         # 允许标注文案自身的长度
        assert out.startswith("H")            # 头部保留（字段名/表头）
        assert out.rstrip().endswith("T")     # 尾部保留
        # 语义必须是「主动截断」而不是「数据缺失」：
        # 实测踩过——报告把「省略日K中段 24,735 字符」错写成「周K/月K 数据缺失」。
        assert "主动截断" in out
        assert "不得写成「数据缺失」" in out
        assert "已被工具取到" in out or "已由工具取到" in out

    def test_ascending_time_series_keeps_recent_tail(self):
        """升序序列（最早在前）——最近的数据在尾部，必须保留。"""
        rows = [{"date": "2025-08-%02d" % (i % 28 + 1), "close": 1.0 + i * 0.01} for i in range(260)]
        content = json.dumps(rows, ensure_ascii=False)
        out = _hard_truncate_keep_both_ends(content, 800)
        assert "2025-08-01" in out[:200]            # 头（最早）在
        assert str(rows[-1]["close"]) in out[-400:]  # 尾（最近）在

    def test_descending_news_keeps_recent_head(self):
        """降序序列（最新在前，如新闻列表）——最近的数据在头部，必须保留。"""
        items = ["最新新闻-A", "次新新闻-B"] + ["旧闻-%03d" % i for i in range(400)]
        content = " | ".join(items)
        out = _hard_truncate_keep_both_ends(content, 300)
        assert "最新新闻-A" in out[:200]

    def test_descending_time_series_keeps_recent_head_dominant(self):
        """降序时序(新→早)：头部(最近)应占大头，早期只留少量。"""
        rows = [{"date": "2026-%02d-%02d" % (m, d), "c": 1.0}
                for m in range(12, 0, -1) for d in range(28, 0, -1)]
        content = json.dumps(rows, ensure_ascii=False)
        out = _hard_truncate_keep_both_ends(content, 800)
        assert "2026-12-28" in out[:600]     # 最近在头部大段
        assert "2026-01-01" in out[-200:]    # 最早只在尾部小段


class TestCompressPromptClasses:
    def test_prompt_has_three_classes(self):
        for marker in ("A. 时间序列类", "B. 列表类", "C. 文本类"):
            assert marker in _COMPRESS_SYSTEM_PROMPT, marker

    def test_prompt_class_specific_guidance(self):
        assert "优先保留最近时间的数据" in _COMPRESS_SYSTEM_PROMPT   # A 类
        assert "保住列表本身" in _COMPRESS_SYSTEM_PROMPT             # B 类：不能把列表概括掉
        assert "龙头" in _COMPRESS_SYSTEM_PROMPT                     # B 类：优先展示
        assert "持仓" in _COMPRESS_SYSTEM_PROMPT                     # B 类：用户相关优先
        assert "信息熵" in _COMPRESS_SYSTEM_PROMPT                   # C 类

    def test_prompt_distinguishes_active_truncation_from_missing(self):
        """主动截断 ≠ 数据缺失（不得把「省略中段」写成「数据缺失」）。"""
        assert "主动截断" in _COMPRESS_SYSTEM_PROMPT
        assert "数据缺失" in _COMPRESS_SYSTEM_PROMPT
        # 也不得由「某段被截断」外推其它周期/字段缺失
        assert "其它周期" in _COMPRESS_SYSTEM_PROMPT

    def test_prompt_is_static_without_goal_placeholder(self):
        """系统提示固定不变（用户目标移入 user 消息）——跨调用/跨运行前缀稳定，缓存友好。"""
        assert "{goal_hint}" not in _COMPRESS_SYSTEM_PROMPT
        assert "{" not in _COMPRESS_SYSTEM_PROMPT   # 禁止任何花括号，防止误用 .format 破坏固定前缀
        assert "}" not in _COMPRESS_SYSTEM_PROMPT
