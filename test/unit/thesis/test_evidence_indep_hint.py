"""重叠样本提示（`_indep_count` + `_verdict_text` 的 n 后缀）。

为什么单独测这条：接口的 n 是「每个交易日一个前瞻窗口」，相邻窗口共享 h-1 天，
有效独立周期只有 n/h 个，显著性被高估约 √h 倍。不点破这一点，页面上 n=270
会被读成 270 个独立观测——2026-11-01 审计时正是这个误读差点导致批量反转阈值表。

实测对照（a_share_regime ① 汇率，2016-2026）：
  重叠口径 n=270 r=+0.43 → 独立采样 n=145 r=+0.157（临界 0.16，不显著）
"""
import pytest

from thesis.evidence import _indep_count, _verdict_text


class TestIndepCount:
    @pytest.mark.parametrize("n,h,expect", [
        (270, 20, 13),     # 270 // 20
        (280, 20, 14),
        (58, 20, 2),
        (48, 60, 1),       # 不足一个窗口也要算 1，不能给 0
        (20, 20, 1),
    ])
    def test_basic(self, n, h, expect):
        assert _indep_count(n, h) == expect

    @pytest.mark.parametrize("n,h", [(None, 20), (270, None), (0, 20), (270, 0),
                                     (270, 1), ("x", 20), (270, "x")])
    def test_meaningless_returns_zero(self, n, h):
        """缺 h 或 h<=1 时不出提示——宁可不提，也不给一个错的窗口数。"""
        assert _indep_count(n, h) == 0


class TestVerdictTextHasHint:
    def _primary(self, **kw):
        base = {"label": "未来 20 个交易日", "corr_value": 0.43, "n": 270, "h": 20,
                "spearman_value": 0.46, "q5_q1": 5.53}
        base.update(kw)
        return base

    def test_hint_follows_n(self):
        text = _verdict_text(self._primary(), {"percentile": 0.0}, {"aligned": None})
        assert "独立窗口约 13 个" in text
        # 必须紧跟在 n 之后，否则读者先看到 n 就已经形成印象了
        assert text.index("n=270") < text.index("独立窗口约 13 个")

    def test_no_hint_when_h_missing(self):
        p = self._primary()
        p.pop("h")
        text = _verdict_text(p, {"percentile": 0.0}, {"aligned": None})
        assert "独立窗口" not in text
        assert "n=270" in text

    def test_direction_warning_still_present(self):
        """加提示不能挤掉原有的方向警告——两者各自回答不同问题。"""
        text = _verdict_text(
            self._primary(),
            {"percentile": 0.0},
            {"aligned": False, "text": "评分方向可能与收益相反"},
        )
        assert "独立窗口约 13 个" in text
        assert "评分方向可能与收益相反" in text

    def test_short_window_branch_unaffected(self):
        """短窗口分支本来就写明「样本不足」，不追加独立窗口数，避免自相矛盾。"""
        text = _verdict_text(
            self._primary(short_window=True, label="未来 5 个交易日", n=400),
            {"percentile": 0.5}, {"aligned": None})
        assert "仅作方向参考" in text
        assert "独立窗口" not in text
