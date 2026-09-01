"""Fetcher 内部配置

注意：此类仅包含 fetcher 专有配置（超时、限流、重试、PyTdx 等）。
与顶层 config.Config 的边界：
- 顶层 Config：全局配置（API Key、Agent 参数、缓存过期等），被 80+ 模块引用
- 本模块 Config：fetcher 专有配置，仅被 tools/fetcher/ 内部文件引用

重复项（DATASOURCE_MAX_FAILS、TUSHARE_TOKEN、AKSHARE_ENABLE_EASTMONEY_PATCH）
在此保留是因为 fetcher 内部高频读取，避免每次 import 顶层 Config 的开销。
如需修改这些值，请同步修改顶层 config.py 的 _ENV_MAP。
"""
import os


class Config:
    # ── 数据源熔断配置 ──
    DATASOURCE_MAX_FAILS = int(os.getenv("DATASOURCE_MAX_FAILS", "5"))
    DATASOURCE_RECOVERY_SECS = int(os.getenv("DATASOURCE_RECOVERY_SECS", "600"))

    # ── Token 配置 ──
    TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")

    # ── 同花顺官方数据服务 ──
    HITHINK_FINANCE_API_KEY = os.environ.get("HITHINK_FINANCE_API_KEY", "")
    HITHINK_PRIORITY = int(os.getenv("HITHINK_PRIORITY", "95"))

    # ── 超时配置 ──
    REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))
    AKSHARE_CALL_TIMEOUT = int(os.getenv("AKSHARE_CALL_TIMEOUT", "30"))

    # ── Akshare 反爬配置 ──
    AKSHARE_RATE_LIMIT_MIN = float(os.getenv("AKSHARE_RATE_LIMIT_MIN", "2.0"))
    AKSHARE_RATE_LIMIT_MAX = float(os.getenv("AKSHARE_RATE_LIMIT_MAX", "5.0"))
    AKSHARE_REALTIME_CACHE_TTL = int(os.getenv("AKSHARE_REALTIME_CACHE_TTL", "1200"))
    AKSHARE_ENABLE_EASTMONEY_PATCH = os.getenv("AKSHARE_ENABLE_EASTMONEY_PATCH", "true").lower() == "true"

    # ── Pytdx 配置 ──
    PYTDX_PRIORITY = int(os.getenv("PYTDX_PRIORITY", "75"))
    PYTDX_CONNECTION_COOLDOWN = int(os.getenv("PYTDX_CONNECTION_COOLDOWN", "15"))
    PYTDX_SERVERS = os.getenv("PYTDX_SERVERS", "")

    # ── 重试配置 ──
    RETRY_MAX_RETRIES = int(os.getenv("RETRY_MAX_RETRIES", "5"))
    RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "2.0"))
    RETRY_MAX_DELAY = float(os.getenv("RETRY_MAX_DELAY", "30.0"))
