"""盯盘告警存储测试 — 临时 DB，纯本地，不依赖外部环境。"""
import threading

import pytest

from server.monitoring.store import MonitorStore


@pytest.fixture
def store(tmp_path):
    return MonitorStore(str(tmp_path / "monitor_alerts.db"))


# ---------------------------------------------------------------------------
# 规则 CRUD
# ---------------------------------------------------------------------------

class TestRuleCRUD:
    def test_add_rule_returns_id_and_list(self, store):
        rule_id = store.add_rule(
            stock_code="600519", stock_name="贵州茅台",
            rule_type="price_below", threshold=1300.0,
        )
        assert isinstance(rule_id, int) and rule_id > 0

        rules = store.list_rules()
        assert len(rules) == 1
        r = rules[0]
        assert r["stock_code"] == "600519"
        assert r["rule_type"] == "price_below"
        assert r["threshold"] == 1300.0
        assert r["enabled"] == 1
        assert r["once_only"] == 1
        assert r["cooldown_s"] == 1800
        assert r["source"] == "manual"  # API 创建默认 manual

    def test_list_rules_enabled_only(self, store):
        id1 = store.add_rule("600519", "贵州茅台", "price_above", 1500.0)
        store.add_rule("000001", "平安银行", "pct_below", -5.0)

        store.update_rule(id1, enabled=0)
        assert len(store.list_rules()) == 2
        assert len(store.list_rules(enabled_only=True)) == 1
        assert store.list_rules(enabled_only=True)[0]["stock_code"] == "000001"

    def test_update_rule_threshold_and_cooldown(self, store):
        rule_id = store.add_rule("600519", "贵州茅台", "price_above", 1500.0)
        assert store.update_rule(rule_id, threshold=1600.0, cooldown_s=600) is True

        r = store.list_rules()[0]
        assert r["threshold"] == 1600.0
        assert r["cooldown_s"] == 600

    def test_update_rule_nonexistent_returns_false(self, store):
        assert store.update_rule(9999, threshold=1.0) is False

    def test_delete_rule(self, store):
        rule_id = store.add_rule("600519", "贵州茅台", "price_above", 1500.0)
        assert store.delete_rule(rule_id) is True
        assert store.list_rules() == []
        assert store.delete_rule(rule_id) is False


# ---------------------------------------------------------------------------
# 告警记录与查询
# ---------------------------------------------------------------------------

class TestAlerts:
    def test_record_and_list_alerts(self, store):
        store.record_alert(1, "600519", "贵州茅台", "价格下穿 1300", price=1298.5, pct=-2.1)
        alerts = store.list_alerts()
        assert len(alerts["items"]) == 1
        assert alerts["total"] == 1
        a = alerts["items"][0]
        assert a["stock_code"] == "600519"
        assert a["rule_desc"] == "价格下穿 1300"
        assert a["price"] == 1298.5
        assert a["read"] == 0

    def test_list_alerts_pagination(self, store):
        for i in range(5):
            store.record_alert(1, "600519", "贵州茅台", f"告警{i}")
        page1 = store.list_alerts(page=1, size=3)
        page2 = store.list_alerts(page=2, size=3)
        assert page1["total"] == 5
        assert len(page1["items"]) == 3
        assert len(page2["items"]) == 2

    def test_list_alerts_desc_order(self, store):
        store.record_alert(1, "600519", "贵州茅台", "第一条")
        store.record_alert(1, "600519", "贵州茅台", "第二条")
        items = store.list_alerts()["items"]
        assert items[0]["rule_desc"] == "第二条"

    def test_unread_filter_and_mark_read(self, store):
        id1 = store.record_alert(1, "600519", "贵州茅台", "a")
        store.record_alert(1, "600519", "贵州茅台", "b")

        assert store.unread_count() == 2
        unread = store.list_alerts(unread=True)["items"]
        assert len(unread) == 2

        assert store.mark_read([id1]) == 1
        assert store.unread_count() == 1
        assert len(store.list_alerts(unread=True)["items"]) == 1


# ---------------------------------------------------------------------------
# 冷却判断
# ---------------------------------------------------------------------------

class TestCooldown:
    def test_recently_fired_within_cooldown(self, store):
        store.record_alert(7, "600519", "贵州茅台", "x")
        assert store.is_recently_fired(7, cooldown_s=1800) is True

    def test_not_fired_when_no_alert(self, store):
        assert store.is_recently_fired(7, cooldown_s=1800) is False

    def test_expired_cooldown(self, store):
        # 手动写入一条 1 小时前的告警
        from datetime import datetime, timedelta
        old = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        store.record_alert(7, "600519", "贵州茅台", "x", triggered_at=old)
        assert store.is_recently_fired(7, cooldown_s=1800) is False

    def test_cooldown_per_rule_isolated(self, store):
        store.record_alert(7, "600519", "贵州茅台", "x")
        assert store.is_recently_fired(8, cooldown_s=1800) is False


# ---------------------------------------------------------------------------
# 连接与并发
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_wal_mode(self, store):
        mode = store.get_conn().execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_thread_local_connections(self, store):
        """跨线程并发读写不报错（threading.local 每线程独立连接）。"""
        errors = []

        def writer():
            try:
                for i in range(10):
                    store.record_alert(1, "600519", "贵州茅台", f"w{i}")
            except Exception as e:  # pragma: no cover
                errors.append(e)

        def reader():
            try:
                for _ in range(10):
                    store.list_alerts()
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=writer) for _ in range(3)] + \
                  [threading.Thread(target=reader) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert store.list_alerts()["total"] == 30

    def test_concurrent_first_writes_on_fresh_db_multiple_rounds(self, tmp_path):
        """全新库上多线程首个写：WAL 索引初始化竞态回归（2026-10-01）

        背景：SQLite 在最后一个连接关闭时会 checkpoint 并删除 `-wal`/`-shm`，
        于是每个新库/每次进程启动后这两个文件都不存在。多线程的首个连接会并发初始化
        WAL 索引，抢输的连接被 SQLite 判为只读 → 写线程抛
        `OperationalError: attempt to write a readonly database`
        （修复前：3 写线程并发 5/5 复现；`busy_timeout` 无效）。

        跑多轮是因为竞态有偶发性；修复前本用例每轮必失败，修复后必须全轮通过。
        """
        rounds = 3
        for r in range(rounds):
            store = MonitorStore(str(tmp_path / f"fresh_{r}.db"))
            errors = []

            def writer():
                try:
                    for i in range(10):
                        store.record_alert(1, "600519", "贵州茅台", f"w{i}")
                except Exception as e:  # pragma: no cover
                    errors.append(e)

            def reader():
                try:
                    for _ in range(10):
                        store.list_alerts()
                except Exception as e:  # pragma: no cover
                    errors.append(e)

            threads = [threading.Thread(target=writer) for _ in range(3)] + \
                      [threading.Thread(target=reader) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert errors == [], f"第 {r + 1} 轮出现并发写错误: {errors}"
            assert store.list_alerts()["total"] == 30

    def test_writable_conn_helper_is_serialized_and_usable(self, tmp_path):
        """建连接助手：返回可用连接且不抛异常（只读介质下读操作也须可用）

        注意**不要**在这里加 `BEGIN IMMEDIATE` 写探针：建连接时取写锁会与
        持有未提交写事务的线程互相阻塞（2026-10-01 实测导致 scheduler run-loop
        测试长时间卡住）。
        """
        from server.monitoring.store import _open_writable_conn

        conn = _open_writable_conn(str(tmp_path / "plain.db"))
        try:
            conn.execute("CREATE TABLE t (x)")
            conn.commit()
            assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        finally:
            conn.close()
