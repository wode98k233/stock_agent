"""港股指数日线采集：列名映射 + 多源降级。

**真实事故（2026-09-18）**：`ak.stock_hk_index_daily_em` 返回的收盘价列名是 `latest`
（不是 `close`/`收盘`），而 `fetch_and_store_index_daily` 的 col_map 没有这条映射 →
**每一条港股指数日线都静默落库成 close=NULL**：行数正常（300 行）、没有任何报错、
`collect` 返回值也正常，只是所有收益率/均线/拥挤度全算不出来。
直到要给恒生科技建台账、读 HSTECH 日线时才发现。

所以这里不测「函数能不能跑」，而是直接测**列名映射**与**源降级顺序**这两件事。
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.cache import market_data_db as mdb  # noqa: E402


def _em_like() -> pd.DataFrame:
    """东财港股指数接口的真实返回形态：date/open/high/low/**latest**，无量额。"""
    return pd.DataFrame({
        "date": ["2026-09-16", "2026-09-17"],
        "open": [4311.70, 4281.03],
        "high": [4337.24, 4313.95],
        "low": [4259.27, 4254.46],
        "latest": [4325.45, 4310.74],
    })


def _sina_like() -> pd.DataFrame:
    """新浪港股指数接口的真实返回形态：含 close 与量额。"""
    return pd.DataFrame({
        "date": ["2026-09-16", "2026-09-17"],
        "open": [4311.70, 4281.03],
        "high": [4337.24, 4313.95],
        "low": [4259.27, 4254.46],
        "close": [4325.4502, 4310.74023],
        "volume": [985705522, 902518526],
        "amount": [49616541802, 43688736863],
    })


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """复刻 fetch_and_store_index_daily 里的 col_map 规范化。

    刻意从源码里取 col_map（而不是在测试里重抄一遍），这样映射被改错时测试会跟着变——
    但也正因如此，下面的 test_col_map_maps_latest_to_close 直接断言**映射表内容**，
    两处一起才能既防「映射被删」又防「规范化逻辑被改坏」。
    """
    col_map = _col_map_from_source()
    out = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    return out


def _col_map_from_source() -> dict:
    """从 market_data_db 源码里抽取 col_map 字面量。

    用正则而不是 import，是因为 col_map 是函数内局部变量。这样断言的是**真实生效的映射**。
    """
    import re
    src = (ROOT / "utils" / "cache" / "market_data_db.py").read_text(encoding="utf-8")
    m = re.search(r"col_map = \{(.*?)\n\s*\}", src, re.S)
    assert m, "未能在源码里找到 col_map"
    pairs = re.findall(r"'([^']+)':\s*'([^']+)'", m.group(1))
    assert pairs, "col_map 解析结果为空"
    return dict(pairs)


class TestColumnMapping:
    def test_col_map_maps_latest_to_close(self):
        """核心断言：`latest` → `close`。删掉这条映射港股指数就全废。"""
        cm = _col_map_from_source()
        assert cm.get("latest") == "close", \
            "col_map 缺 'latest' → 'close'：东财港股指数接口的收盘价列名就是 latest"

    def test_col_map_still_maps_the_a_share_paths(self):
        """补映射不能碰坏 A 股/中证那条路径。"""
        cm = _col_map_from_source()
        for src_name, dst in [("date", "trade_date"), ("report_date", "trade_date"),
                              ("日期", "trade_date"), ("close", "close"),
                              ("收盘", "close"), ("open", "open"), ("high", "high"),
                              ("low", "low"), ("volume", "volume"), ("amount", "amount")]:
            assert cm.get(src_name) == dst, (src_name, dst)

    def test_em_payload_normalizes_close(self):
        """东财形态（latest）规范化后必须有 close，且值等于 latest。"""
        out = _normalize(_em_like())
        assert "close" in out.columns, "latest 未被映射成 close"
        assert list(out["close"]) == [4325.45, 4310.74]
        assert "trade_date" in out.columns
        # 东财不提供量额 → 列缺失属源限制，不应凭空造出来
        assert "volume" not in out.columns

    def test_sina_payload_normalizes_close_and_keeps_amounts(self):
        out = _normalize(_sina_like())
        assert list(out["close"]) == [4325.4502, 4310.74023]
        assert "volume" in out.columns and "amount" in out.columns
        assert out["amount"].notna().all()


class TestSourceFallback:
    """新浪优先、东财兜底：东财每次调用都要先分页拉 spot 建代码字典，受限网络下极易被掐断。"""

    def test_prefers_sina_when_both_work(self):
        calls = []

        class FakeAk:
            @staticmethod
            def stock_hk_index_daily_sina(symbol):
                calls.append(("sina", symbol))
                return _sina_like()

            @staticmethod
            def stock_hk_index_daily_em(symbol):
                calls.append(("em", symbol))
                return _em_like()

        df = mdb._fetch_hk_index_daily(FakeAk, "HSTECH")
        assert calls == [("sina", "HSTECH")], "应只调新浪，不该再打东财"
        assert "close" in df.columns

    def test_falls_back_to_em_when_sina_fails(self):
        calls = []

        class FakeAk:
            @staticmethod
            def stock_hk_index_daily_sina(symbol):
                calls.append("sina")
                raise ConnectionError("proxy closed")

            @staticmethod
            def stock_hk_index_daily_em(symbol):
                calls.append("em")
                return _em_like()

        df = mdb._fetch_hk_index_daily(FakeAk, "HSI")
        assert calls == ["sina", "em"]
        assert not df.empty

    def test_falls_back_to_em_when_sina_returns_empty(self):
        class FakeAk:
            @staticmethod
            def stock_hk_index_daily_sina(symbol):
                return pd.DataFrame()

            @staticmethod
            def stock_hk_index_daily_em(symbol):
                return _em_like()

        df = mdb._fetch_hk_index_daily(FakeAk, "HSI")
        assert not df.empty

    def test_raises_when_all_sources_fail(self):
        class FakeAk:
            @staticmethod
            def stock_hk_index_daily_sina(symbol):
                raise ConnectionError("proxy")

            @staticmethod
            def stock_hk_index_daily_em(symbol):
                raise ConnectionError("spot map failed")

        with pytest.raises(RuntimeError) as ei:
            mdb._fetch_hk_index_daily(FakeAk, "HSTECH")
        msg = str(ei.value)
        assert "HSTECH" in msg
        assert "新浪" in msg and "东财" in msg, "错误信息要能看出两个源都试过了"


class TestHkCodeDetection:
    """HSTECH/HSI 走港股分支，H30269 必须走中证分支（历史踩坑）。"""

    def test_hstech_is_treated_as_hk_not_csindex(self):
        from tools.fetcher.akshare_ds import _is_csindex_code
        assert _is_csindex_code("HSTECH") is False
        assert _is_csindex_code("HSI") is False
        assert _is_csindex_code("H30269") is True

    def test_hk_branch_is_selected_for_hstech(self):
        """复刻 fetch_and_store_index_daily 里的分支判定。"""
        from tools.fetcher.akshare_ds import _is_csindex_code
        for code in ("HSTECH", "HSI"):
            is_csindex = _is_csindex_code(code)
            is_hk = (not is_csindex) and code.startswith(("H", "hk"))
            assert is_hk is True, code

    def test_csindex_code_does_not_take_the_hk_branch(self):
        from tools.fetcher.akshare_ds import _is_csindex_code
        code = "H30269"
        is_hk = (not _is_csindex_code(code)) and code.startswith(("H", "hk"))
        assert is_hk is False, "H30269 是红利低波（中证），不能走港股接口"

    def test_symbol_is_not_lowercase_stripped(self):
        """`code.lstrip('hk')` 只剥离小写 hk 前缀；'HSTECH' 必须原样传给数据源。"""
        code = "HSTECH"
        symbol = code.lstrip("hk") if code.startswith("hk") else code
        assert symbol == "HSTECH"
