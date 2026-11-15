"""backtrader utcnow 弃用告警抑制（2026-10-15 批量回测刷屏事故）。

背景：``cerebro.run()`` 每根 K 线触发 ``datetime.utcnow()`` 弃用告警，
500+ 次批量回测时若 warnings 过滤器被第三方库运行期改宽，stderr 会被刷成
上千行（经 ``utils/io_redirect`` 全按 ERROR 落日志，实测单次 run 1027 行）。

``backtest.silence_backtrader_warnings()`` 做定向抑制（只压 backtrader 模块、
只压 DeprecationWarning），engine / signal_scanner 在每次 ``cerebro.run()`` 前
重插一次 —— 因为第三方库会把过滤器条目插到列表前面，import 时插一次会被压住。
"""
import re
import warnings


def test_helper_suppresses_backtrader_keeps_others():
    """宽过滤器（模拟被改宽）先入列，helper 重插后仍能压住 backtrader 告警。"""
    from backtest import silence_backtrader_warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")     # 模拟第三方把过滤器改宽
        silence_backtrader_warnings()       # run 前的重插（prepend）
        warnings.warn_explicit(
            "datetime.datetime.utcnow() is deprecated",
            DeprecationWarning, "cerebro.py", 1543, module="backtrader.cerebro")
        warnings.warn_explicit(
            "other deprecation", DeprecationWarning,
            "other.py", 1, module="project.other")

    msgs = [str(w.message) for w in caught]
    assert "datetime.datetime.utcnow() is deprecated" not in msgs, "backtrader 弃用告警必须被抑制"
    assert "other deprecation" in msgs, "非 backtrader 的告警不得被误伤"


def test_filter_entry_matches_only_backtrader_module():
    """结构化断言：抑制条目 = ignore + DeprecationWarning + 仅匹配 backtrader 前缀。"""
    from backtest import silence_backtrader_warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        silence_backtrader_warnings()
        entry = warnings.filters[0]
        assert entry[0] == "ignore"
        assert entry[2] is DeprecationWarning
        pat = entry[3]
        match = pat.match if hasattr(pat, "match") else re.compile(pat).match
        assert match("backtrader")
        assert match("backtrader.cerebro")
        assert not match("backtraderx")     # 前缀必须到点号或结尾，不能误伤同名包
        assert not match("project.backtrader")


def test_helper_is_idempotent():
    """重复调用只追加等价条目，不应抛错（engine 每次 run 都会调）。"""
    from backtest import silence_backtrader_warnings

    silence_backtrader_warnings()
    silence_backtrader_warnings()
