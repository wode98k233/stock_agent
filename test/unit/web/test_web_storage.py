from pathlib import Path


def test_web_storage_persists_dialogs_and_messages(tmp_path):
    from server.storage import WebStorage

    db_path = tmp_path / "web.db"
    storage = WebStorage(db_path)

    dialog = storage.create_dialog(title="新对话", mode="react_stock")
    user_msg = storage.create_message(
        dialog_uuid=dialog["dialog_uuid"],
        role="user",
        content="分析贵州茅台",
        status="completed",
        mode="react_stock",
    )
    assistant_msg = storage.create_message(
        dialog_uuid=dialog["dialog_uuid"],
        role="assistant",
        content="",
        status="streaming",
        mode="react_stock",
        task_id="task-1",
    )

    storage.update_message(
        assistant_msg["message_uuid"],
        content="模拟回答",
        status="completed",
        log_uuid="log-1",
        log_file="logs/log-1.log",
        trace_run_id="trace-1",
    )

    reopened = WebStorage(db_path)
    dialogs = reopened.list_dialogs()
    messages = reopened.list_messages(dialog["dialog_uuid"])

    assert Path(db_path).exists()
    assert dialogs[0]["dialog_uuid"] == dialog["dialog_uuid"]
    assert user_msg["message_uuid"] == messages[0]["message_uuid"]
    assert messages[1]["content"] == "模拟回答"
    assert messages[1]["status"] == "completed"
    assert messages[1]["log_uuid"] == "log-1"
    assert messages[1]["trace_run_id"] == "trace-1"


def test_latest_trace_runs_for_dialogs_returns_latest_and_handles_chunks(tmp_path, monkeypatch):
    from server import storage as storage_mod
    from server.storage import WebStorage

    monkeypatch.setattr(storage_mod, "_SQLITE_PARAM_CHUNK_SIZE", 2)
    storage = WebStorage(tmp_path / "web.db")
    dialogs = [
        storage.create_dialog(title=f"dialog-{i}", mode="react_stock")
        for i in range(3)
    ]

    storage.create_message(
        dialog_uuid=dialogs[0]["dialog_uuid"],
        role="assistant",
        content="old",
        status="completed",
        trace_run_id="trace-old",
    )
    storage.create_message(
        dialog_uuid=dialogs[0]["dialog_uuid"],
        role="assistant",
        content="new",
        status="completed",
        trace_run_id="trace-new",
    )
    storage.create_message(
        dialog_uuid=dialogs[1]["dialog_uuid"],
        role="assistant",
        content="empty",
        status="completed",
        trace_run_id="",
    )
    storage.create_message(
        dialog_uuid=dialogs[2]["dialog_uuid"],
        role="assistant",
        content="only",
        status="completed",
        trace_run_id="trace-third",
    )

    result = storage.latest_trace_runs_for_dialogs([d["dialog_uuid"] for d in dialogs])

    assert result == {
        dialogs[0]["dialog_uuid"]: "trace-new",
        dialogs[2]["dialog_uuid"]: "trace-third",
    }


def test_web_storage_marks_unfinished_assistant_messages_failed(tmp_path):
    from server.storage import WebStorage

    storage = WebStorage(tmp_path / "web.db")
    dialog = storage.create_dialog(title="新对话", mode="pdor")
    streaming = storage.create_message(
        dialog_uuid=dialog["dialog_uuid"],
        role="assistant",
        content="",
        status="streaming",
        mode="pdor",
        task_id="task-2",
    )

    affected = storage.mark_interrupted_messages()
    messages = storage.list_messages(dialog["dialog_uuid"])

    assert affected == 1
    assert messages[0]["message_uuid"] == streaming["message_uuid"]
    assert messages[0]["status"] == "failed"
    assert "server 已重启" in messages[0]["error"]


# ── WatchlistStorage 测试 ──

def test_watchlist_add_and_list(tmp_path):
    from server.storage import WebStorage

    storage = WebStorage(tmp_path / "web.db")
    wl = storage.watchlist

    wl.add_stock("600519", "贵州茅台", "cn", "local", "自选股")
    wl.add_stock("000001", "平安银行", "cn", "mx", "自选股")

    items = wl.list_stocks()
    assert len(items) == 2
    codes = {i["stock_code"] for i in items}
    assert codes == {"600519", "000001"}


def test_watchlist_remove(tmp_path):
    from server.storage import WebStorage

    storage = WebStorage(tmp_path / "web.db")
    wl = storage.watchlist

    wl.add_stock("600519", "贵州茅台", "cn")
    wl.add_stock("000001", "平安银行", "cn")

    assert wl.remove_stock("600519", "cn") is True
    assert wl.remove_stock("999999", "cn") is False

    items = wl.list_stocks()
    assert len(items) == 1
    assert items[0]["stock_code"] == "000001"


def test_watchlist_upsert_on_duplicate(tmp_path):
    from server.storage import WebStorage

    storage = WebStorage(tmp_path / "web.db")
    wl = storage.watchlist

    wl.add_stock("600519", "贵州茅台", "cn", source="local")
    wl.add_stock("600519", "茅台", "cn", source="mx")

    items = wl.list_stocks()
    assert len(items) == 1
    assert items[0]["stock_name"] == "茅台"
    assert items[0]["source"] == "mx"


def test_watchlist_price_fields(tmp_path):
    from server.storage import WebStorage

    storage = WebStorage(tmp_path / "web.db")
    wl = storage.watchlist

    wl.add_stock("600519", "贵州茅台", "cn", source="mx",
                 price=1800.0, change_pct=2.5, change_amt=44.0,
                 high_price=1810.0, low_price=1780.0,
                 turnover_rate=0.35, volume_ratio=1.2,
                 volume="1.2万", trading_amount="21.6亿",
                 pe=30.5, pb=8.2,
                 total_market_value="2.26万亿", circulation_market_value="2.26万亿")

    items = wl.list_stocks()
    assert len(items) == 1
    item = items[0]
    assert item["price"] == 1800.0
    assert item["change_pct"] == 2.5
    assert item["change_amt"] == 44.0
    assert item["high_price"] == 1810.0
    assert item["low_price"] == 1780.0
    assert item["turnover_rate"] == 0.35
    assert item["volume_ratio"] == 1.2
    assert item["volume"] == "1.2万"
    assert item["trading_amount"] == "21.6亿"
    assert item["pe"] == 30.5
    assert item["pb"] == 8.2
    assert item["total_market_value"] == "2.26万亿"
    assert item["circulation_market_value"] == "2.26万亿"


def test_watchlist_bulk_replace(tmp_path):
    from server.storage import WebStorage

    storage = WebStorage(tmp_path / "web.db")
    wl = storage.watchlist

    # 初始 MX 数据
    stocks = [
        {"stock_code": "600519", "stock_name": "贵州茅台", "market": "cn", "price": 1800.0, "change_pct": 2.5},
        {"stock_code": "000001", "stock_name": "平安银行", "market": "cn", "price": 12.5, "change_pct": -0.8},
    ]
    count = wl.bulk_replace(stocks, source="mx")
    assert count == 2

    items = wl.list_stocks()
    assert len(items) == 2

    # 再次同步，旧的 mx 数据应被替换
    stocks2 = [
        {"stock_code": "600519", "stock_name": "贵州茅台", "market": "cn", "price": 1850.0, "change_pct": 3.0},
    ]
    wl.bulk_replace(stocks2, source="mx")

    items = wl.list_stocks()
    assert len(items) == 1
    assert items[0]["price"] == 1850.0


def test_watchlist_bulk_replace_preserves_local(tmp_path):
    from server.storage import WebStorage

    storage = WebStorage(tmp_path / "web.db")
    wl = storage.watchlist

    # 手动添加一只
    wl.add_stock("300750", "宁德时代", "cn", source="local")
    # MX 同步
    wl.bulk_replace([{"stock_code": "600519", "stock_name": "贵州茅台", "market": "cn"}], source="mx")

    items = wl.list_stocks()
    assert len(items) == 2
    sources = {i["stock_code"]: i["source"] for i in items}
    assert sources["300750"] == "local"
    assert sources["600519"] == "mx"


def test_watchlist_filter_by_tag(tmp_path):
    from server.storage import WebStorage

    storage = WebStorage(tmp_path / "web.db")
    wl = storage.watchlist

    wl.add_stock("600519", "贵州茅台", "cn", tags="自选股")
    wl.add_stock("000001", "平安银行", "cn", tags="持仓")

    items = wl.list_stocks(tag="自选股")
    assert len(items) == 1
    assert items[0]["stock_code"] == "600519"

    items = wl.list_stocks(tag="持仓")
    assert len(items) == 1
    assert items[0]["stock_code"] == "000001"

    items = wl.list_stocks()
    assert len(items) == 2


def test_watchlist_persistence(tmp_path):
    from server.storage import WebStorage

    db_path = tmp_path / "web.db"
    storage = WebStorage(db_path)
    storage.watchlist.add_stock("600519", "贵州茅台", "cn", price=1800.0)

    # 重新打开，数据应持久化
    storage2 = WebStorage(db_path)
    items = storage2.watchlist.list_stocks()
    assert len(items) == 1
    assert items[0]["stock_code"] == "600519"
    assert items[0]["price"] == 1800.0


def test_watchlist_update_quote(tmp_path):
    from server.storage import WebStorage

    storage = WebStorage(tmp_path / "web.db")
    wl = storage.watchlist

    # 先添加一只（source=local）
    wl.add_stock("300502", "新易盛", "cn", source="local", tags="自选股")

    # 更新行情
    ok = wl.update_quote("300502", "cn", {
        "name": "新易盛",
        "price": 706.45,
        "change_pct": -1.655,
        "change_amt": -11.85,
        "high": 733.99,
        "low": 701.01,
        "turnover_rate": 3.748,
        "pe": 34.52,
        "pb": 6.61,
        "total_mv": "7036亿",
        "circ_mv": "7036亿",
    })
    assert ok is True

    items = wl.list_stocks()
    assert len(items) == 1
    item = items[0]
    # source 不应被改动
    assert item["source"] == "local"
    assert item["tags"] == "自选股"
    # 行情已更新
    assert item["price"] == 706.45
    assert item["change_pct"] == -1.655
    assert item["high_price"] == 733.99
    assert item["low_price"] == 701.01
    assert item["pe"] == 34.52
    assert item["total_market_value"] == "7036亿"


def test_watchlist_update_quote_nonexistent(tmp_path):
    from server.storage import WebStorage

    storage = WebStorage(tmp_path / "web.db")
    ok = storage.watchlist.update_quote("999999", "cn", {"price": 100.0})
    assert ok is False
