"""盯盘调度器：asyncio 后台循环 + 线程池隔离阻塞调用。

设计约束（详见 docs/superpowers/specs/2026-09-05-market-monitor-design.md §3.2）：
1. 轮询体必须走 asyncio.to_thread——新浪批量报价是同步 requests 调用，
   直接在协程里调会冻结 event loop（所有 SSE/请求停摆）
2. 单轮整体超时 20s，超时跳过本轮（防轮次堆积），_poll_lock 防并发重入
3. 交易时段判断在循环层（is_market_open × is_trading_hours），
   poll_once 本身不判时段——供 API/e2e 在非交易时段手动触发
"""
import asyncio
import logging
import threading
import time
from datetime import datetime

from utils.trading_calendar import is_market_open, is_trading_hours

from .notifier import AlertNotifier
from .quotes import SinaBatchQuotes
from .rules import evaluate_all
from .store import MonitorStore

logger = logging.getLogger(__name__)

FAIL_ALERT_STREAK = 5  # 连续失败 N 轮记一条系统告警
RULES_CACHE_TTL_S = 60.0  # 规则缓存，避免每轮读库
POLL_TIMEOUT_S = 20.0  # 单轮整体超时


class MonitorScheduler:
    """盯盘轮询调度器：自选股全量 → 批量报价 → 规则判定 → 推送 + 落库。"""

    def __init__(self, storage=None, store: MonitorStore | None = None,
                 quotes: SinaBatchQuotes | None = None,
                 notifier: AlertNotifier | None = None):
        self._storage = storage  # WebStorage，盘中只读自选股
        self._store = store or MonitorStore()
        self._quotes = quotes or SinaBatchQuotes()
        self._notifier = notifier or AlertNotifier()

        self._prev_quotes: dict[str, dict] = {}
        self._fail_streak = 0
        self._last_poll_at: str | None = None
        self._last_duration_s: float | None = None
        self._rules_cache: list[dict] | None = None
        self._rules_cached_at = 0.0
        self._poll_lock = threading.Lock()

    # -- 主循环 ------------------------------------------------------------

    async def run(self, interval: int = 60,
                  poll_timeout: float = POLL_TIMEOUT_S) -> None:
        """asyncio 后台循环：交易时段内每 interval 秒轮询一轮。"""
        while True:
            try:
                if is_market_open("cn") and is_trading_hours("cn"):
                    await asyncio.wait_for(
                        asyncio.to_thread(self.poll_once), timeout=poll_timeout)
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, TimeoutError):
                logger.warning("[盯盘] 单轮超时 %.0fs，跳过本轮", poll_timeout)
            except Exception as e:
                logger.warning("[盯盘] 轮询失败（本轮跳过）: %s", e)
            await asyncio.sleep(interval)

    # -- 单轮轮询（同步，被 to_thread 调用；也可供 API 手动触发） ------------

    def poll_once(self) -> dict:
        """执行一轮：报价 → 规则判定（穿越语义）→ 冷却过滤 → 推送 + 落库。"""
        if not self._poll_lock.acquire(blocking=False):
            return {"skipped": "poll_in_progress"}
        try:
            t0 = time.monotonic()
            codes = self._watchlist_codes()
            quotes = self._quotes.fetch(codes) if codes else {}
            if not quotes:
                result = self._on_fetch_failed()
            else:
                self._fail_streak = 0
                result = self._evaluate_and_fire(quotes)
            self._last_poll_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._last_duration_s = time.monotonic() - t0
            return result
        finally:
            self._poll_lock.release()

    def _evaluate_and_fire(self, quotes: dict) -> dict:
        rules = self._enabled_rules()
        skip = {r["id"] for r in rules
                if self._store.is_recently_fired(r["id"], r["cooldown_s"])}
        alerts = evaluate_all(quotes, rules, self._prev_quotes, skip_rule_ids=skip)
        rule_map = {r["id"]: r for r in rules}
        for alert in alerts:
            channels_ok = self._notifier.push(alert)
            self._store.record_alert(
                rule_id=alert["rule_id"], stock_code=alert["stock_code"],
                stock_name=alert["stock_name"], rule_desc=alert["rule_desc"],
                price=alert["price"], pct=alert["pct"], channels_ok=channels_ok)
            rule = rule_map.get(alert["rule_id"], {})
            if rule.get("once_only"):
                self._store.update_rule(alert["rule_id"], enabled=0)
                # 缓存同步移除，防止冷却窗短于缓存 TTL 时重复评估
                self._rules_cache = [r for r in (self._rules_cache or [])
                                     if r["id"] != alert["rule_id"]]
        self._prev_quotes = quotes
        return {"quotes": len(quotes), "alerts": len(alerts), "fired": alerts}

    def _on_fetch_failed(self) -> dict:
        self._fail_streak += 1
        if self._fail_streak == FAIL_ALERT_STREAK:
            desc = f"盯盘行情连续失败 {self._fail_streak} 轮，请检查网络或新浪接口"
            self._store.record_alert(None, "SYSTEM", "系统", desc)
            logger.error("[盯盘] %s", desc)
        return {"quotes": 0, "alerts": 0, "fail_streak": self._fail_streak}

    def _watchlist_codes(self) -> list[str]:
        """盘中读自选股（现有表，只读），仅 A 股。"""
        if self._storage is None:
            return []
        try:
            stocks = self._storage.watchlist.list_stocks()
        except Exception:
            logger.debug("[盯盘] 读自选股失败", exc_info=True)
            return []
        return [s["stock_code"] for s in stocks if s.get("market", "cn") == "cn"]

    def _enabled_rules(self) -> list[dict]:
        now = time.monotonic()
        if self._rules_cache is None or now - self._rules_cached_at > RULES_CACHE_TTL_S:
            self._rules_cache = self._store.list_rules(enabled_only=True)
            self._rules_cached_at = now
        return self._rules_cache

    # -- 状态查询（API 用） --------------------------------------------------

    def get_status(self) -> dict:
        try:
            watchlist_count = len(self._watchlist_codes())
        except Exception:
            watchlist_count = 0
        return {
            "last_poll_at": self._last_poll_at,
            "last_duration_s": self._last_duration_s,
            "enabled_rules": len(self._enabled_rules()),
            "fail_streak": self._fail_streak,
            "watchlist_count": watchlist_count,
        }


# 进程级单例：bootstrap 启动时注册，API 路由层经 get_scheduler() 读取状态/手动触发
_scheduler_instance: "MonitorScheduler | None" = None


def set_scheduler(s: "MonitorScheduler | None") -> None:
    global _scheduler_instance
    _scheduler_instance = s


def get_scheduler() -> "MonitorScheduler | None":
    return _scheduler_instance
