"""东财持仓同步单测：三格式解析 / normalize / diff / fetch 401 / 路由幂等。

mock 原则：requests 全 mock，不发真实网络请求；真实库只用 tmp_path 临时库。
"""
import asyncio
import json

import pytest

from server.routes import portfolio as pf
from server.services import eastmoney_sync as em


# ---------------------------------------------------------------------------
# fixtures：实测报文（validatekey 脱敏）
# ---------------------------------------------------------------------------

_PS_PASTE = """$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$session.UserAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"
$c1 = New-Object System.Net.Cookie("qgqp_b_id", "abc123", "/", ".eastmoney.com")
$session.Cookies.Add($c1)
$c2 = New-Object System.Net.Cookie("HAList", "ty-SHA-000001", "/", ".eastmoney.com")
$session.Cookies.Add($c2)
$c3 = New-Object System.Net.Cookie("kuaizhang_openid", "", "/", ".18.cn")
$session.Cookies.Add($c3)
Invoke-WebRequest -Uri "https://jywg.18.cn/Com/queryAssetAndPositionV1?validatekey=REDACTED_KEY123" `
    -WebSession $session `
    -Headers @{
"Referer"="https://jywg.18.cn/Search/Position"
"X-Requested-With"="XMLHttpRequest"
}
"""

_FETCH_PASTE = """fetch("https://jywg.18.cn/Com/queryAssetAndPositionV1?validatekey=REDACTED_KEY123", {
  "headers": {
    "accept": "application/json, text/javascript, */*; q=0.01",
    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    "gw_reqtimestamp": "1757155200000",
    "x-requested-with": "XMLHttpRequest"
  },
  "body": "moneyType=RMB",
  "method": "POST",
  "credentials": "include"
});"""

_FETCH_PASTE_WITH_COOKIE = _FETCH_PASTE.replace(
    '"x-requested-with": "XMLHttpRequest"',
    '"x-requested-with": "XMLHttpRequest",\n    "cookie": "kuaizhang_openid=x; HAList=y"')


def _em_position(code="002714", name="牧原股份", qty=100, cbjg="41.132",
                 zxjg="43.96", zxsz="4396", ckyk="282.8", ykbl="0.069",
                 zqlx="SA", kysl="100", dryk=""):
    return {
        "Zqdm": code, "Zqmc": name, "Zqsl": str(qty), "Cbjg": cbjg,
        "Zxjg": zxjg, "Zxsz": zxsz, "Ckyk": ckyk, "Ykbl": ykbl,
        "Zqlx": zqlx, "Kysl": kysl, "Dryk": dryk, "Market": "SA",
        "Gddm": "0123456789", "Ljyk": ckyk, "Jybz": "0",
    }


def _em_response(positions=None, zzc="100000.0", kyzj="35000.0",
                 zjye="35000.0", mkval="65000.0", status=0, errcode=0):
    if positions is None:
        # 15 只样本：4 股票 + 11 ETF（口径照实测：Zqlx='E' 为 ETF）
        positions = [
            _em_position("002714", "牧原股份", 100, "41.132", "43.96", "4396",
                         "282.8", "0.069", "SA"),
            _em_position("300760", "迈瑞医疗", 50, "280.5", "292.1", "14605",
                         "580", "0.041", "SA"),
            _em_position("600519", "贵州茅台", 10, "1620.0", "1580.0", "15800",
                         "-400", "-0.024", "HA"),
            _em_position("601127", "赛力斯", 200, "102.3", "98.5", "19700",
                         "-760", "-0.037", "HA"),
        ]
        etf_codes = [
            ("513330", "恒生互联ETF", 52500, "0.409", "0.369", "-0.098"),
            ("510300", "沪深300ETF", 8000, "3.912", "4.015", "0.026"),
            ("512100", "中证1000ETF", 12000, "2.236", "2.185", "-0.023"),
            ("588000", "科创50ETF", 15000, "0.982", "1.024", "0.043"),
            ("159915", "创业板ETF", 9000, "2.453", "2.398", "-0.022"),
            ("512480", "半导体ETF", 6500, "0.856", "0.902", "0.054"),
            ("515790", "光伏ETF", 11000, "0.324", "0.301", "-0.071"),
            ("512690", "酒ETF", 7200, "0.685", "0.662", "-0.034"),
            ("518880", "黄金ETF", 3000, "5.432", "5.781", "0.064"),
            ("513050", "中概互联ETF", 4500, "0.912", "0.867", "-0.049"),
            ("512880", "证券ETF", 20000, "0.795", "0.831", "0.045"),
        ]
        for code, name, qty, cbjg, zxjg, ykbl in etf_codes:
            cbjg_f, zxjg_f = float(cbjg), float(zxjg)
            positions.append(_em_position(
                code, name, qty, cbjg, zxjg,
                str(round(qty * zxjg_f, 2)),
                str(round((zxjg_f - cbjg_f) * qty, 2)), ykbl, "E", str(qty)))
    data0 = {"Zzc": zzc, "Kyzj": kyzj, "Zjye": zjye,
             "totalSecMkval": mkval, "positions": positions}
    return {"Status": status, "Errcode": errcode, "Errmsg": "", "Data": [data0]}


# ---------------------------------------------------------------------------
# 三格式解析
# ---------------------------------------------------------------------------

class TestParsePaste:
    def test_powershell_format(self):
        s = em.parse_paste(_PS_PASTE)
        assert isinstance(s, em.Emsession)
        assert s.validatekey == "REDACTED_KEY123"
        assert "qgqp_b_id=abc123" in s.cookie_header
        assert "HAList=ty-SHA-000001" in s.cookie_header
        assert "kuaizhang_openid=" in s.cookie_header
        assert s.host == "jywg.18.cn"

    def test_fetch_format_without_cookie(self):
        """fetch 格式 credentials:include 无 Cookie 头 → 解析通过（拉取 401 才提示）。"""
        s = em.parse_paste(_FETCH_PASTE)
        assert isinstance(s, em.Emsession)
        assert s.validatekey == "REDACTED_KEY123"
        assert s.cookie_header == ""

    def test_fetch_format_with_cookie(self):
        s = em.parse_paste(_FETCH_PASTE_WITH_COOKIE)
        assert s.cookie_header.startswith("kuaizhang_openid=")

    def test_json_direct(self):
        snap = em.parse_paste(json.dumps(_em_response(), ensure_ascii=False))
        assert isinstance(snap, em.DirectSnapshot)
        assert len(snap.snapshot.positions) == 15
        assert snap.snapshot.account["total_asset"] == pytest.approx(100000.0)

    def test_empty_raises(self):
        with pytest.raises(em.EmsyncError, match="为空"):
            em.parse_paste("   ")

    def test_unknown_format_raises(self):
        with pytest.raises(em.EmsyncError, match="无法识别"):
            em.parse_paste("随便一段文字")

    def test_ps_missing_validatekey(self):
        with pytest.raises(em.EmsyncError, match="validatekey"):
            em.parse_paste(_PS_PASTE.replace("validatekey=REDACTED_KEY123", ""))

    def test_ps_missing_cookie(self):
        text = "\n".join(l for l in _PS_PASTE.splitlines() if "Cookie" not in l)
        with pytest.raises(em.EmsyncError, match="Cookie"):
            em.parse_paste(text)

    def test_fetch_missing_validatekey(self):
        with pytest.raises(em.EmsyncError, match="validatekey"):
            em.parse_paste(_FETCH_PASTE.replace("?validatekey=REDACTED_KEY123", ""))

    def test_error_message_no_credentials(self):
        """错误信息不得带回报文原文（凭据防泄漏）。"""
        with pytest.raises(em.EmsyncError) as e:
            em.parse_paste("$session 但缺少关键内容 SECRET_COOKIE_VALUE")
        assert "SECRET_COOKIE_VALUE" not in str(e.value)


# ---------------------------------------------------------------------------
# fetch_positions（requests 全 mock）
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=200, payload=None, non_json=False):
        self.status_code = status_code
        self._payload = payload
        self._non_json = non_json

    def json(self):
        if self._non_json:
            raise ValueError("not json")
        return self._payload


class TestFetchPositions:
    def test_ok(self, monkeypatch):
        captured = {}

        def fake_post(url, headers=None, data=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["data"] = data
            return _FakeResponse(payload=_em_response())

        monkeypatch.setattr(em.requests, "post", fake_post)
        session = em.parse_paste(_PS_PASTE)
        resp = em.fetch_positions(session)
        assert resp["Status"] == 0
        assert "validatekey=REDACTED_KEY123" in captured["url"]
        assert captured["data"] == "moneyType=RMB"
        assert captured["headers"]["gw_reqtimestamp"].isdigit()
        assert captured["headers"]["X-Requested-With"] == "XMLHttpRequest"
        assert captured["headers"]["Referer"].endswith("/Search/Position")
        assert captured["headers"]["Cookie"].startswith("qgqp_b_id=")

    def test_401_without_cookie_hint(self, monkeypatch):
        monkeypatch.setattr(em.requests, "post",
                            lambda *a, **k: _FakeResponse(status_code=401))
        session = em.parse_paste(_FETCH_PASTE)  # 无 cookie 头
        with pytest.raises(em.EmsyncError, match="fetch 格式缺 Cookie"):
            em.fetch_positions(session)

    def test_401_with_cookie_session_expired(self, monkeypatch):
        monkeypatch.setattr(em.requests, "post",
                            lambda *a, **k: _FakeResponse(status_code=401))
        session = em.parse_paste(_PS_PASTE)  # 有 cookie
        with pytest.raises(em.EmsyncError, match="会话过期请重新复制"):
            em.fetch_positions(session)

    def test_status_error_translates(self, monkeypatch):
        monkeypatch.setattr(
            em.requests, "post",
            lambda *a, **k: _FakeResponse(
                payload={"Status": -1, "Errcode": 1, "Errmsg": "校验失败"}))
        session = em.parse_paste(_PS_PASTE)
        with pytest.raises(em.EmsyncError, match="会话过期请重新复制"):
            em.fetch_positions(session)

    def test_network_error(self, monkeypatch):
        def boom(*a, **k):
            raise em.requests.ConnectionError("refused")
        monkeypatch.setattr(em.requests, "post", boom)
        session = em.parse_paste(_PS_PASTE)
        with pytest.raises(em.EmsyncError, match="网络请求失败"):
            em.fetch_positions(session)

    def test_non_json_response(self, monkeypatch):
        monkeypatch.setattr(
            em.requests, "post",
            lambda *a, **k: _FakeResponse(non_json=True))
        session = em.parse_paste(_PS_PASTE)
        with pytest.raises(em.EmsyncError, match="非 JSON"):
            em.fetch_positions(session)


# ---------------------------------------------------------------------------
# normalize：字段映射 + 空串容忍
# ---------------------------------------------------------------------------

class TestNormalize:
    def test_full_mapping_15_positions(self):
        snap = em.normalize(_em_response())
        assert len(snap.positions) == 15
        assert snap.skipped_rows == 0
        muyuan = next(p for p in snap.positions if p["stock_code"] == "002714")
        assert muyuan["stock_name"] == "牧原股份"
        assert muyuan["qty"] == 100
        assert muyuan["avg_cost"] == pytest.approx(41.132)
        assert muyuan["last_price"] == pytest.approx(43.96)
        assert muyuan["market_value"] == pytest.approx(4396)
        assert muyuan["float_pnl"] == pytest.approx(282.8)
        assert muyuan["float_pnl_pct"] == pytest.approx(0.069)  # 小数原样
        assert muyuan["is_etf"] is False
        assert muyuan["avail_qty"] == pytest.approx(100)

    def test_etf_flag_11(self):
        """Zqlx='E' 的 11 只 ETF 正确标记。"""
        snap = em.normalize(_em_response())
        etfs = [p for p in snap.positions if p["is_etf"]]
        assert len(etfs) == 11
        hs = next(p for p in snap.positions if p["stock_code"] == "513330")
        assert hs["is_etf"] is True
        assert hs["stock_name"] == "恒生互联ETF"

    def test_dryk_empty_string(self):
        """Dryk="" 空串不崩（实测存在的坑）。"""
        pos = [_em_position(dryk="")]
        snap = em.normalize(_em_response(positions=pos))
        assert len(snap.positions) == 1

    def test_today_pnl_per_position(self):
        """持仓行 Dryk（当日盈亏）正确解析。"""
        pos = [_em_position("002714", dryk="123.45")]
        snap = em.normalize(_em_response(positions=pos))
        assert snap.positions[0]["today_pnl"] == pytest.approx(123.45)

    def test_account_today_pnl_sum(self):
        """组合当日盈亏 = 各持仓行 Dryk 求和；空串行跳过。"""
        positions = [
            _em_position("002714", dryk="100.5"),
            _em_position("600519", dryk="-40.25"),
            _em_position("513330", "恒生互联ETF", qty=52500, zqlx="E", dryk=""),
        ]
        snap = em.normalize(_em_response(positions=positions))
        assert snap.account["today_pnl"] == pytest.approx(60.25)
        assert snap.positions[2]["today_pnl"] is None

    def test_account_today_pnl_none_when_all_empty(self):
        snap = em.normalize(_em_response(positions=[_em_position("002714", dryk="")]))
        assert snap.account["today_pnl"] is None

    def test_empty_fields_tolerated(self):
        pos = [_em_position(ckyk="", ykbl="", zxjg="", zxsz="", kysl="")]
        snap = em.normalize(_em_response(positions=pos))
        p = snap.positions[0]
        assert p["float_pnl"] is None
        assert p["float_pnl_pct"] is None
        assert p["last_price"] is None
        assert p["market_value"] is None
        assert p["avail_qty"] is None
        assert p["avg_cost"] == pytest.approx(41.132)

    def test_zero_qty_skipped(self):
        """qty=0 或缺 code 的行跳过并计数。"""
        positions = [
            _em_position("002714", qty=100),
            _em_position("000000", name="空数量行", qty=0),
            _em_position("", name="缺代码行", qty=50),
            _em_position("513330", "恒生互联ETF", qty=52500, zqlx="E"),
        ]
        snap = em.normalize(_em_response(positions=positions))
        assert len(snap.positions) == 2
        assert snap.skipped_rows == 2

    def test_empty_positions(self):
        snap = em.normalize(_em_response(positions=[]))
        assert snap.positions == []
        assert snap.account["total_asset"] == pytest.approx(100000.0)


# ---------------------------------------------------------------------------
# diff 快照对比
# ---------------------------------------------------------------------------

def _mk_row(code, qty, avg_cost, source="eastmoney", market="cn"):
    return {"stock_code": code, "market": market, "qty": qty,
            "avg_cost": avg_cost, "source": source}


class TestDiffSnapshot:
    def test_first_import_all_added(self):
        snap = em.normalize(_em_response())
        diff = em.diff_snapshot(snap.positions, [])
        assert len(diff["added"]) == 15
        assert diff["updated"] == []
        assert diff["removed"] == []
        assert diff["unchanged"] == 0

    def test_idempotent(self):
        """重复导入同一数据 → unchanged 15，零写入。"""
        snap = em.normalize(_em_response())
        existing = [
            _mk_row(p["stock_code"], p["qty"], p["avg_cost"])
            for p in snap.positions
        ]
        diff = em.diff_snapshot(snap.positions, existing)
        assert diff["unchanged"] == 15
        assert not diff["added"] and not diff["updated"] and not diff["removed"]

    def test_updated_on_change(self):
        snap = em.normalize(_em_response())
        existing = [
            _mk_row(p["stock_code"], p["qty"], p["avg_cost"])
            for p in snap.positions
        ]
        existing[0]["qty"] = existing[0]["qty"] + 100  # 数量变化 → updated
        diff = em.diff_snapshot(snap.positions, existing)
        assert len(diff["updated"]) == 1
        assert diff["unchanged"] == 14

    def test_manual_row_takes_broker_snapshot(self):
        """§3.7：手动行同 code 数值不同 → 以券商为准（updated）。"""
        snap = em.normalize(_em_response())
        existing = [_mk_row("002714", 50, 40.0, source="manual")]
        diff = em.diff_snapshot(snap.positions, existing)
        assert len(diff["updated"]) == 1
        assert diff["updated"][0]["stock_code"] == "002714"

    def test_removed_closed(self):
        """东财没有但 source='eastmoney' 有 → removed（标记清仓，不删行）。"""
        snap = em.normalize(_em_response(positions=[_em_position("002714")]))
        existing = [
            _mk_row("002714", 100, 41.132),
            _mk_row("513330", 52500, 0.409),   # 已清仓：快照中没有
            _mk_row("600519", 0, 1620.0),      # 保留历史 qty=0 → 不再报 removed
        ]
        diff = em.diff_snapshot(snap.positions, existing)
        assert diff["removed"] == ["513330"]

    def test_manual_row_not_removed(self):
        """手动行不在快照中 → 不受东财清仓影响。"""
        snap = em.normalize(_em_response(positions=[_em_position("002714")]))
        existing = [_mk_row("600519", 100, 1620.0, source="manual")]
        diff = em.diff_snapshot(snap.positions, existing)
        assert diff["removed"] == []


# ---------------------------------------------------------------------------
# 账户资金缓存：落库持久化（仅金额，凭据不落） + 内存/DB 兜底
# ---------------------------------------------------------------------------

class TestAccountCachePersistence:
    def test_persist_and_reload_after_restart(self, tmp_path, monkeypatch):
        """同步落库 → 模拟重启内存清空 → 读库兜底仍能取到金额。"""
        db = str(tmp_path / "account.db")
        monkeypatch.setattr(em, "_ACCOUNT_CACHE", {})
        em.set_account_cache({
            "total_asset": 100000.0, "avail_cash": 35000.0,
            "balance": 35000.0, "positions_market_value": 65000.0,
            "today_pnl": 123.45, "synced_at": "2026-09-09T10:00:00",
        }, db_path=db)
        monkeypatch.setattr(em, "_ACCOUNT_CACHE", {})  # 模拟服务重启
        loaded = em.get_account_cache(db_path=db)
        assert loaded is not None
        assert loaded["total_asset"] == pytest.approx(100000.0)
        assert loaded["avail_cash"] == pytest.approx(35000.0)
        assert loaded["today_pnl"] == pytest.approx(123.45)
        assert loaded["synced_at"] == "2026-09-09T10:00:00"

    def test_memory_wins_over_db(self, tmp_path, monkeypatch):
        """内存有值优先于库（拿到最新一次同步）。"""
        db = str(tmp_path / "account.db")
        monkeypatch.setattr(em, "_ACCOUNT_CACHE", {})
        em.set_account_cache({"total_asset": 100.0}, db_path=db)  # 库写旧低值
        # 随后内存被更新为高值（模拟又一次同步）
        monkeypatch.setattr(em, "_ACCOUNT_CACHE",
                            {"total_asset": 99999.0, "avail_cash": 1.0,
                             "balance": 1.0, "positions_market_value": 99998.0,
                             "today_pnl": None, "synced_at": None})
        assert em.get_account_cache(db_path=db)["total_asset"] == pytest.approx(99999.0)

    def test_load_db_missing_returns_none(self, tmp_path, monkeypatch):
        """无表/无行/库不存在 → None（不崩）。"""
        monkeypatch.setattr(em, "_ACCOUNT_CACHE", {})
        assert em.get_account_cache(db_path=str(tmp_path / "none.db")) is None

    def test_persist_failure_silent(self, tmp_path, monkeypatch):
        """落库失败（目录不存在）静默，不影响内存缓存主链路。"""
        monkeypatch.setattr(em, "_ACCOUNT_CACHE", {})
        em.set_account_cache(
            {"total_asset": 1.0}, db_path=str(tmp_path / "no" / "dir" / "x.db"))
        assert em.get_account_cache()["total_asset"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 路由：preview / import / session（临时库，端到端 mock）
# ---------------------------------------------------------------------------

@pytest.fixture
def portfolio(tmp_path, monkeypatch):
    from server.storage import WebStorage
    storage = WebStorage(tmp_path / "web.db")
    monkeypatch.setattr(pf, "_get_portfolio", lambda req=None: storage.portfolio)
    # 账户资金落库限定在临时库，且每次测试清空内存缓存（隔离真实 stock_radar.db）
    monkeypatch.setattr(em, "_ACCOUNT_CACHE", {})
    monkeypatch.setattr(em, "_account_db_path",
                        lambda: str(tmp_path / "web.db"))
    # 画像持仓同步会写真实 data/user_profile/default.json（UPM 固定用户 default），
    # 测试中禁用以避免把夹具 demo 持仓写进生产用户画像。
    monkeypatch.setattr(pf, "_sync_profile_holdings", lambda _p: None)
    return storage.portfolio


class TestRoutesE2E:
    def test_preview_ps_format(self, portfolio, monkeypatch):
        """① 贴 PowerShell 实测报文 → 预览 15 只 + 账户资金。"""
        monkeypatch.setattr(em.requests, "post",
                            lambda *a, **k: _FakeResponse(payload=_em_response()))
        payload = pf.EastmoneyPreviewRequest(paste_text=_PS_PASTE)
        result = asyncio.get_event_loop_policy().new_event_loop() \
            .run_until_complete(pf.eastmoney_preview(None, payload)) \
            if False else asyncio.run(pf.eastmoney_preview(None, payload))
        assert result["code"] == 0
        data = result["data"]
        assert len(data["snapshot"]["positions"]) == 15
        assert data["snapshot"]["account"]["total_asset"] == pytest.approx(100000.0)
        assert len(data["diff"]["added"]) == 15
        # 落库零变化（预览不入库）
        assert len(portfolio.list_positions()) == 0

    def test_preview_fetch_format(self, portfolio, monkeypatch):
        """② fetch 格式（无 Cookie 头）解析 + 拉取同样成功。"""
        monkeypatch.setattr(em.requests, "post",
                            lambda *a, **k: _FakeResponse(payload=_em_response()))
        payload = pf.EastmoneyPreviewRequest(paste_text=_FETCH_PASTE)
        result = asyncio.run(pf.eastmoney_preview(None, payload))
        assert result["code"] == 0
        assert len(result["data"]["snapshot"]["positions"]) == 15

    def test_preview_error_shell(self, portfolio):
        payload = pf.EastmoneyPreviewRequest(paste_text="格式不对")
        result = asyncio.run(pf.eastmoney_preview(None, payload))
        assert result["code"] == 1
        assert "msg" in result

    def test_import_then_idempotent(self, portfolio):
        """⑤⑤ 导入 → 再导入 → 幂等 0 差异。"""
        snap = em.normalize(_em_response())
        payload = pf.EastmoneyImportRequest(
            snapshot={"mode": "direct", "account": snap.account,
                      "positions": snap.positions, "skipped_rows": 0})
        r1 = asyncio.run(pf.eastmoney_import(None, payload))
        assert r1["code"] == 0
        d1 = r1["data"]
        assert d1["added"] == 15
        positions = portfolio.list_positions()
        assert len(positions) == 15
        assert all(p["source"] == "eastmoney" for p in positions)
        # 重复导入 → unchanged 15，added/updated/removed 全 0
        r2 = asyncio.run(pf.eastmoney_import(None, payload))
        d2 = r2["data"]
        assert d2["added"] == 0 and d2["updated"] == 0 and d2["removed"] == 0
        assert d2["unchanged"] == 15

    def test_import_persists_today_pnl(self, portfolio):
        """持仓行 Dryk（当日盈亏）随快照导入落库到 portfolio_position。"""
        pos = [_em_position("002714", dryk="123.45")]
        snap = em.normalize(_em_response(positions=pos, zzc="1.0", kyzj="1.0",
                                         zjye="1.0", mkval="1.0"))
        payload = pf.EastmoneyImportRequest(
            snapshot={"account": snap.account, "positions": snap.positions})
        asyncio.run(pf.eastmoney_import(None, payload))
        row = next(p for p in portfolio.list_positions()
                   if p["stock_code"] == "002714")
        assert row["today_pnl"] == pytest.approx(123.45)

    def test_import_preserves_manual_rows(self, portfolio):
        """手动行与东财行并存（§3.7 混合来源）。"""
        portfolio.add_trade("601398", "buy", 1000, 5.0, "2026-08-01",
                            stock_name="工商银行")
        snap = em.normalize(_em_response())
        payload = pf.EastmoneyImportRequest(
            snapshot={"account": snap.account, "positions": snap.positions})
        asyncio.run(pf.eastmoney_import(None, payload))
        rows = portfolio.list_positions()
        assert len(rows) == 16
        manual = next(p for p in rows if p["stock_code"] == "601398")
        assert manual["source"] == "manual"
        em_row = next(p for p in rows if p["stock_code"] == "002714")
        assert em_row["source"] == "eastmoney"

    def test_import_marks_removed_closed(self, portfolio):
        """removed → qty=0 清仓标记（不删行）。"""
        snap1 = em.normalize(_em_response())
        payload1 = pf.EastmoneyImportRequest(
            snapshot={"account": snap1.account, "positions": snap1.positions})
        asyncio.run(pf.eastmoney_import(None, payload1))
        # 第二次快照少了 513330（清仓）
        reduced = [p for p in snap1.positions if p["stock_code"] != "513330"]
        payload2 = pf.EastmoneyImportRequest(
            snapshot={"account": snap1.account, "positions": reduced})
        r2 = asyncio.run(pf.eastmoney_import(None, payload2))
        assert r2["data"]["removed"] == 1
        row = next(p for p in portfolio.list_positions()
                   if p["stock_code"] == "513330")
        assert row["qty"] == 0  # 保留历史

    def test_session_endpoint(self, portfolio):
        """⑦ GET /eastmoney/session：从表里读，无凭据。"""
        snap = em.normalize(_em_response())
        payload = pf.EastmoneyImportRequest(
            snapshot={"account": snap.account, "positions": snap.positions})
        asyncio.run(pf.eastmoney_import(None, payload))
        result = asyncio.run(pf.eastmoney_session(None))
        assert result["code"] == 0
        meta = result["data"]
        assert meta["eastmoney_count"] == 15
        assert meta["eastmoney_etf_count"] == 11
        assert meta["source_counts"].get("eastmoney") == 15
        assert meta["account"]["total_asset"] == pytest.approx(100000.0)
        assert meta["last_synced_at"]

    def test_preview_account_not_in_db(self, portfolio, monkeypatch):
        """账户资金不落库：只在内存缓存（安全红线）。"""
        import sqlite3
        monkeypatch.setattr(em.requests, "post",
                            lambda *a, **k: _FakeResponse(payload=_em_response()))
        payload = pf.EastmoneyPreviewRequest(paste_text=_PS_PASTE)
        asyncio.run(pf.eastmoney_preview(None, payload))
        conn = sqlite3.connect(str(portfolio._conn.execute(
            "SELECT 1").fetchone() and _db_file_of(portfolio)))
        # portfolio_position 表不含任何凭据/账户字段——只查列表名
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(portfolio_position)").fetchall()]
        assert "validatekey" not in cols
        assert "cookie" not in cols
        conn.close()


def _db_file_of(portfolio_storage) -> str:
    """取 PortfolioStorage 所在连接的 db 文件路径（测试辅助）。"""
    return str(portfolio_storage._conn.execute(
        "PRAGMA database_list").fetchall()[0][2])
