"""ETF 标的识别测试（P4：别名词典 + 前缀规则）。"""
import sqlite3

from signal_eval.symbols import resolve_etf_alias, etf_symbol_key


def _make_info_db(tmp_path, etfs):
    info_db = tmp_path / "market_data.db"
    c = sqlite3.connect(str(info_db))
    c.execute("CREATE TABLE stock_info (code TEXT, name TEXT, market TEXT)")
    for code, name in etfs:
        c.execute("INSERT INTO stock_info VALUES (?,?,?)", (code, name, "ETF"))
    c.commit()
    c.close()
    return info_db


def test_etf_symbol_key_prefix():
    """裸代码 → symbol_key 前缀规则（5 开头 sh / 1 开头 sz）。"""
    assert etf_symbol_key("510880") == "sh510880"
    assert etf_symbol_key("159928") == "sz159928"
    assert etf_symbol_key("600519") is None   # 个股代码不走 ETF 通道
    assert etf_symbol_key("52") is None       # 短码


def test_resolve_etf_alias_hardcoded():
    """硬编码高频简称直接命中。"""
    assert resolve_etf_alias("红利ETF") is not None
    assert resolve_etf_alias("消费ETF") is not None
    assert resolve_etf_alias("黄金ETF") is not None
    assert resolve_etf_alias("创业板ETF") is not None
    assert resolve_etf_alias("沪深300ETF") is not None
    # 命中结果是合法 symbol_key
    k = resolve_etf_alias("红利ETF")
    assert k and k[:2] in ("sh", "sz") and len(k) == 8


def test_resolve_etf_alias_case_insensitive():
    """大小写不敏感（etf/Etf/ETF）。"""
    assert resolve_etf_alias("红利etf") == resolve_etf_alias("红利ETF")


def test_resolve_etf_alias_by_name_lookup(tmp_path, monkeypatch):
    """词典未命中 → 按基金名规则匹配本地 stock_info ETF 行。"""
    info_db = _make_info_db(tmp_path, [
        ("512890", "华泰柏瑞中证红利低波动交易型开放式指数证券投资基金"),
        ("510880", "上证红利交易型开放式指数证券投资基金"),
    ])
    monkeypatch.setattr("signal_eval.symbols.STOCK_INFO_DB_PATH", str(info_db))
    monkeypatch.setattr("signal_eval.symbols._alias_cache", {})
    # 「低波红利ETF」含关键词「红利低波动」→ 匹配 512890
    k = resolve_etf_alias("红利低波动ETF")
    assert k == "sh512890"


def test_resolve_etf_alias_unknown_returns_none(tmp_path, monkeypatch):
    """无匹配返回 None（不误标）。"""
    info_db = _make_info_db(tmp_path, [])
    monkeypatch.setattr("signal_eval.symbols.STOCK_INFO_DB_PATH", str(info_db))
    monkeypatch.setattr("signal_eval.symbols._alias_cache", {})
    assert resolve_etf_alias("不存在的ETF") is None


def test_extract_symbol_resolves_etf(tmp_path, monkeypatch):
    """extract_symbol 全链路：文本含 ETF 简称（无 6 位代码）→ ETF symbol_key。"""
    from signal_eval.extract import extract_symbol
    info_db = _make_info_db(tmp_path, [
        ("510880", "上证红利交易型开放式指数证券投资基金"),
    ])
    monkeypatch.setattr("signal_eval.extract.STOCK_INFO_DB_PATH", str(info_db))
    monkeypatch.setattr("signal_eval.extract._name_cache", {})
    assert extract_symbol("红利ETF现在能买吗") == "sh510880"


def test_extract_symbol_stock_takes_priority(tmp_path, monkeypatch):
    """个股中文名优先于 ETF 词典（避免误吞）。"""
    from signal_eval.extract import extract_symbol
    info_db = tmp_path / "market_data.db"
    c = sqlite3.connect(str(info_db))
    c.execute("CREATE TABLE stock_info (code TEXT, name TEXT, market TEXT)")
    c.execute("INSERT INTO stock_info VALUES ('sh600519','贵州茅台','')")
    c.commit()
    c.close()
    monkeypatch.setattr("signal_eval.extract.STOCK_INFO_DB_PATH", str(info_db))
    monkeypatch.setattr("signal_eval.extract._name_cache", {})
    assert extract_symbol("贵州茅台怎么走") == "sh600519"


# ── ETF 优先级 + 全量目录识别（09-08：stock_info 灌 1600+ ETF 后） ──

def test_extract_symbol_etf_keyword_wins_over_stock(tmp_path, monkeypatch):
    """文本显式含 "ETF" → ETF 通道优先于个股名称匹配。

    回归：灌全量 ETF 后「机器人ETF可以买吗」曾被同名个股「机器人」(300024)
    抢先命中。通用词个股简称与 ETF 简称大量同名，ETF 字样必须先匹配。
    """
    from signal_eval.extract import extract_symbol
    info_db = _make_info_db(tmp_path, [
        ("159770", "机器人ETF天弘"),
    ])
    # 个股「机器人」也在名称表里（模拟真实库）
    c = sqlite3.connect(str(info_db))
    c.execute("INSERT INTO stock_info VALUES ('300024','机器人','')")
    c.commit()
    c.close()
    monkeypatch.setattr("signal_eval.extract.STOCK_INFO_DB_PATH", str(info_db))
    monkeypatch.setattr("signal_eval.extract._name_cache", {})
    monkeypatch.setattr("signal_eval.extract._name_map", {})
    monkeypatch.setattr("signal_eval.extract._name_map_path", None)
    import signal_eval.symbols as sym
    monkeypatch.setattr("signal_eval.symbols.STOCK_INFO_DB_PATH", str(info_db))
    monkeypatch.setattr("signal_eval.symbols._alias_cache", {})
    monkeypatch.setattr("signal_eval.symbols._etf_rows_cache", None)
    monkeypatch.setattr("signal_eval.symbols._etf_rows_path", None)

    # 含 ETF 字样 → 走 ETF 通道命中基金
    assert extract_symbol("机器人ETF可以买吗") == "sz159770"
    # 不含 ETF 字样 → 个股路径正常
    assert extract_symbol("机器人这个股怎么样") == "sz300024"


def test_list_etf_catalog_normalizes_codes(monkeypatch):
    """list_etf_catalog：新浪通道代码带 sh/sz 前缀 → 归一化为裸 6 位码。"""
    import types
    import pandas as pd
    import tools.fund_data as fd

    fake_df = pd.DataFrame([
        {"代码": "sz159998", "名称": "计算机ETF天弘"},
        {"代码": "sh510880", "名称": "红利ETF"},
        {"代码": "bad", "名称": "非法行"},
    ])
    fake_ak = types.SimpleNamespace(
        fund_etf_category_sina=lambda symbol: fake_df)
    monkeypatch.setitem(__import__("sys").modules, "akshare", fake_ak)
    out = fd.list_etf_catalog(logger=__import__("logging").getLogger("test"))
    assert {"code": "159998", "name": "计算机ETF天弘", "market": "sz"} in out
    assert {"code": "510880", "name": "红利ETF", "market": "sh"} in out
    assert all(len(x["code"]) == 6 and x["code"].isdigit() for x in out)


def test_list_etf_catalog_fallback_to_em(monkeypatch):
    """新浪失败 → 降级东财通道。"""
    import types
    import pandas as pd
    import tools.fund_data as fd

    def sina_boom(symbol):
        raise RuntimeError("sina down")

    fake_ak = types.SimpleNamespace(
        fund_etf_category_sina=sina_boom,
        fund_etf_spot_em=lambda: pd.DataFrame([{"代码": "517770", "名称": "游戏传媒ETF浦银"}]))
    monkeypatch.setitem(__import__("sys").modules, "akshare", fake_ak)
    out = fd.list_etf_catalog(logger=__import__("logging").getLogger("test"))
    assert out == [{"code": "517770", "name": "游戏传媒ETF浦银", "market": "sh"}]


def test_list_etf_catalog_all_fail_returns_empty(monkeypatch):
    """两个通道全挂 → 返回空列表（不抛错，灌库任务安全退出）。"""
    import types
    import tools.fund_data as fd

    fake_ak = types.SimpleNamespace(
        fund_etf_category_sina=lambda symbol: (_ for _ in ()).throw(RuntimeError("x")),
        fund_etf_spot_em=lambda: (_ for _ in ()).throw(RuntimeError("y")))
    monkeypatch.setitem(__import__("sys").modules, "akshare", fake_ak)
    assert fd.list_etf_catalog(logger=__import__("logging").getLogger("test")) == []
