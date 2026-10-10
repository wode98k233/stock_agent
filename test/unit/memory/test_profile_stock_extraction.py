"""用户画像个股提取回归：不得把免责声明/修饰/泛指词当股票名。

事故（PRD §3.4）：注入字段出现「用户近期关注个股：关键、不构成、专业、资深、不构成任何」
——`KNOWN_SUFFIXES` 含「投资」等通用词，正则 `([一-龥]{2,6})(?:suffix)`
把「不构成任何**投资**建议」「专业**投资**」抽成「不构成任何」「专业」。
"""
from memory.user_profile import UserProfileManager


def _extract(text):
    # 纯函数测试：绕过 __init__ 的 IO（extract_*_regex 不依赖实例状态）
    mgr = UserProfileManager.__new__(UserProfileManager)
    return mgr._extract_stocks_regex(text, [])


def test_disclaimer_not_extracted():
    text = "仅供参考，不构成任何投资建议。本报告由资深专业团队撰写，关键结论如下。"
    assert _extract(text) == []


def test_common_qualifier_before_suffix_not_extracted():
    assert _extract("专业投资、稳健投资、长期投资、价值投资") == []


def test_real_stock_stem_kept():
    got = _extract("腾讯控股、比亚迪股份、宁德时代")
    assert "腾讯" in got
    assert "比亚迪" in got


def test_prefix_verb_not_included_in_stem():
    # 贪婪匹配会把「我看好腾讯控股」抽成「我看好腾讯」，属于污染，必须拒绝
    assert _extract("我看好腾讯控股") == []


def test_empty_text():
    assert _extract("") == []
