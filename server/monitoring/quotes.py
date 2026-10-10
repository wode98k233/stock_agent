"""新浪批量报价通道（盯盘专用轻量封装，不走 fetcher 重试链）。

为什么不用 fetcher 体系：盯盘轮询是固定频率的后台外呼，若与用户请求共享
DataSourceManager 的限流器/重试链，会互相挤占（akshare 令牌桶 10 次/分钟）。
新浪批量接口单请求可带数百只、实测 0.14s/5只，是盯盘的正确通道。

防封措施：Referer 头（新浪反爬强制）+ 批间最小间隔。
"""
import logging
import re
import time

import requests

logger = logging.getLogger(__name__)

# 新浪行情行：var hq_str_sh600519="名称,今开,昨收,最新价,最高,最低,...,日期,时间,状态";
_LINE_RE = re.compile(r'hq_str_(?:sh|sz)(\d{6})="([^"]*)"', re.IGNORECASE)
# A 股协议字段下标：0名称 1今开 2昨收 3最新价 4最高 5最低 8成交量(股) 9成交额(元)
#                  30日期 31时间
_IDX_NAME, _IDX_OPEN, _IDX_PRE_CLOSE, _IDX_PRICE, _IDX_HIGH, _IDX_LOW = 0, 1, 2, 3, 4, 5
_IDX_VOLUME, _IDX_AMOUNT = 8, 9
_IDX_DATE, _IDX_TIME = 30, 31


def _to_sina_code(code: str) -> str:
    """纯 6 位代码 → 新浪前缀代码（6/9/5 开头沪市，其余深市）。"""
    s = code.strip().lower()
    if s.startswith(("sh", "sz")):
        return s
    return f"sh{s}" if s.startswith(("6", "9", "5")) else f"sz{s}"


def _safe_float(v: str) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


class SinaBatchQuotes:
    """新浪批量报价：单请求数百只，超出分批，批间隔防封。"""

    BATCH_SIZE = 400
    MIN_INTERVAL_S = 1.0
    TIMEOUT_S = 10.0

    _HEADERS = {
        "Referer": "https://finance.sina.com.cn",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0.0.0 Safari/537.36"),
    }

    def __init__(self):
        self._last_request_ts = 0.0

    def fetch(self, codes: list[str]) -> dict[str, dict]:
        """批量拉取报价。

        Returns:
            {纯6位代码: quote}，请求失败或响应缺失的代码不在结果中。
            quote 字段：name/price/pre_close/pct/high/low/open/volume/amount/date/time
        """
        if not codes:
            return {}

        results: dict[str, dict] = {}
        # 去重 + 转 sina 前缀码（输入支持纯 6 位或带前缀，统一归一为 sh/sz+6位）
        seen: dict[str, str] = {}  # sina_code -> 纯 6 位代码
        for c in codes:
            sina = _to_sina_code(c.strip())
            if len(sina) == 8:
                seen.setdefault(sina, sina[2:])

        sina_codes = list(seen.keys())
        for i in range(0, len(sina_codes), self.BATCH_SIZE):
            batch = sina_codes[i:i + self.BATCH_SIZE]
            self._throttle()
            try:
                resp = requests.get(
                    f"https://hq.sinajs.cn/list={','.join(batch)}",
                    headers=self._HEADERS, timeout=self.TIMEOUT_S)
                if resp.status_code != 200:
                    logger.warning("[盯盘] 新浪批量报价 HTTP %s（本轮跳过）",
                                   resp.status_code)
                    continue
                text = resp.content.decode("gbk", errors="replace")
            except Exception as e:
                logger.warning("[盯盘] 新浪批量报价请求失败（本轮跳过）: %s", e)
                continue
            results.update(self._parse(text, seen))

        return results

    def _throttle(self) -> None:
        """同域名批间最小间隔，防止触发反爬封禁。"""
        now = time.monotonic()
        wait = self.MIN_INTERVAL_S - (now - self._last_request_ts)
        if wait > 0:
            time.sleep(wait)
        self._last_request_ts = time.monotonic()

    @staticmethod
    def _parse(text: str, seen: dict[str, str]) -> dict[str, dict]:
        quotes: dict[str, dict] = {}
        for match in _LINE_RE.finditer(text):
            code6, payload = match.group(1), match.group(2)
            parts = payload.split(",")
            if len(parts) <= _IDX_TIME or not parts[_IDX_NAME]:
                continue
            price = _safe_float(parts[_IDX_PRICE]) or 0.0
            pre_close = _safe_float(parts[_IDX_PRE_CLOSE]) or 0.0
            pct = None
            if price > 0 and pre_close > 0:
                pct = (price - pre_close) / pre_close * 100
            quotes[code6] = {
                "code": code6,
                "name": parts[_IDX_NAME],
                "price": price,
                "pre_close": pre_close,
                "pct": pct,
                "open": _safe_float(parts[_IDX_OPEN]),
                "high": _safe_float(parts[_IDX_HIGH]),
                "low": _safe_float(parts[_IDX_LOW]),
                "volume": _safe_float(parts[_IDX_VOLUME]),
                "amount": _safe_float(parts[_IDX_AMOUNT]),
                "date": parts[_IDX_DATE] if len(parts) > _IDX_DATE else None,
                "time": parts[_IDX_TIME] if len(parts) > _IDX_TIME else None,
            }
        return quotes
