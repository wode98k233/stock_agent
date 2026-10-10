"""东方财富持仓同步服务（docs/eastmoney持仓同步-设计方案.md v3 落地）。

三格式粘贴解析（PowerShell / fetch / JSON 直通）→ queryAssetAndPositionV1 拉取
→ normalize（字段映射 + 空串容忍）→ diff 快照对比。

安全红线：Cookie / validatekey 仅在内存对象中存活（预览→确认 ≤5 分钟窗口），
不落库、不写日志。落库的只有持仓数据 + source='eastmoney'。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

EM_BASE = "https://jywg.18.cn"
QUERY_PATH = "/Com/queryAssetAndPositionV1"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class EmsyncError(Exception):
    """东财同步业务错误（信息面向最终用户，中文，不包含任何凭据内容）。"""


# ---------------------------------------------------------------------------
# 数据对象
# ---------------------------------------------------------------------------

@dataclass
class Emsession:
    """粘贴报文解析出的会话凭据（仅内存存活，禁止序列化/落库/写日志）。"""

    validatekey: str
    cookie_header: str = ""   # "k=v; k2=v2" 形式；fetch 格式可能为空（见 401 提示）
    host: str = "jywg.18.cn"


@dataclass
class DirectSnapshot:
    """JSON 响应体直通模式：无需凭据、无需网络，直接携带快照数据。"""

    snapshot: "Emsnapshot"


@dataclass
class Emsnapshot:
    """normalize 后的快照：账户资金 + 持仓行（含东财原始口径的浮盈亏）。"""

    positions: list[dict] = field(default_factory=list)
    account: dict = field(default_factory=dict)
    skipped_rows: int = 0
    mode: str = "session"   # session=凭据拉取 / direct=JSON 直通


# 内存级账户资金缓存（进程内共享给 portfolio_query 技能、面板、agent 注入）。
# 每次同步同时落库 portfolio_account（仅金额，不含任何凭据）：
# 服务重启/跨进程（CLI/TUI）也能读到最近一次同步的账户资金。
_ACCOUNT_CACHE: dict = {}


def _account_db_path() -> str:
    """账户资金落库位置 = 主库 stock_radar.db（延迟导入避免循环依赖）。"""
    from utils.app_paths import get_db_path
    return get_db_path()


def _snapshot_context() -> dict:
    """同步时点的交易状态标注：本地时间 + 盘中/已收盘/休市。

    盘中最新价是实时快照、会随行情波动，只有买入价与数量是确定值——
    该标注让展示层/agent 能如实向用户说明数据时效语义。
    """
    try:
        from datetime import datetime
        from utils.trading_calendar import is_market_open, is_trading_hours
        now = datetime.now().astimezone()
        snapshot_at = now.strftime("%Y-%m-%d %H:%M:%S")
        if not is_market_open("cn", now.date()):
            status = "休市"
        elif is_trading_hours("cn", now):
            status = "盘中"
        else:
            status = "已收盘"
        return {"snapshot_at": snapshot_at, "market_status": status}
    except Exception:
        return {"snapshot_at": "", "market_status": ""}


def set_account_cache(account: dict, db_path: str | None = None) -> None:
    """记录最近一次同步的账户资金（Zzc/Kyzj/Zjye/当日盈亏），内存 + 落库。"""
    global _ACCOUNT_CACHE
    clean = {
        "total_asset": account.get("total_asset"),
        "avail_cash": account.get("avail_cash"),
        "balance": account.get("balance"),
        "positions_market_value": account.get("positions_market_value"),
        "today_pnl": account.get("today_pnl"),
        "synced_at": account.get("synced_at"),
    }
    clean.update(_snapshot_context())
    _ACCOUNT_CACHE = clean
    _persist_account(clean, db_path=db_path)


def get_account_cache(db_path: str | None = None) -> dict | None:
    """读取账户资金缓存；内存为空时兜底读库（跨进程/重启可用）。"""
    if _ACCOUNT_CACHE:
        return _ACCOUNT_CACHE
    return _load_account_db(db_path=db_path)


def _persist_account(account: dict, db_path: str | None = None) -> None:
    """账户金额落库 portfolio_account（仅金额，cookie/validatekey 仍不落库）。
    落库失败静默容忍：内存缓存不受影响，不阻断同步主链路。
    """
    try:
        path = db_path or _account_db_path()
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            # 建表/补列走统一 schema（stock_radar 组的 portfolio_account）
            from utils.db_schema.migrator import ensure_schema
            from utils.db_schema.tables.stock_radar import TABLES
            ensure_schema(conn, [t for t in TABLES if t.name == "portfolio_account"])
            conn.execute(
                """INSERT INTO portfolio_account
                   (key,total_asset,avail_cash,balance,positions_market_value,
                    today_pnl,synced_at,updated_at,snapshot_at,market_status)
                   VALUES('default',?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                       total_asset=excluded.total_asset,
                       avail_cash=excluded.avail_cash,
                       balance=excluded.balance,
                       positions_market_value=excluded.positions_market_value,
                       today_pnl=excluded.today_pnl,
                       synced_at=excluded.synced_at,
                       updated_at=excluded.updated_at,
                       snapshot_at=excluded.snapshot_at,
                       market_status=excluded.market_status""",
                (account.get("total_asset"), account.get("avail_cash"),
                 account.get("balance"), account.get("positions_market_value"),
                 account.get("today_pnl"), account.get("synced_at"),
                 datetime.now(timezone.utc).isoformat(),
                 account.get("snapshot_at"), account.get("market_status")))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _load_account_db(db_path: str | None = None) -> dict | None:
    """读库兜底：取最近一次落库的账户金额；无表/无行/损坏 → None。

    兼容旧表：无 snapshot_at/market_status 列时按旧字段读取（两个标注返回 None）。
    """
    try:
        path = db_path or _account_db_path()
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            cols = {row["name"] for row in conn.execute(
                "PRAGMA table_info(portfolio_account)").fetchall()}
            if "snapshot_at" in cols:
                row = conn.execute(
                    "SELECT total_asset,avail_cash,balance,positions_market_value,"
                    " today_pnl,synced_at,snapshot_at,market_status"
                    " FROM portfolio_account"
                    " WHERE key='default'").fetchone()
            else:
                row = conn.execute(
                    "SELECT total_asset,avail_cash,balance,positions_market_value,"
                    " today_pnl,synced_at FROM portfolio_account"
                    " WHERE key='default'").fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {
            "total_asset": row[0],
            "avail_cash": row[1],
            "balance": row[2],
            "positions_market_value": row[3],
            "today_pnl": row[4],
            "synced_at": row[5],
            "snapshot_at": row[6] if len(row) > 6 else None,
            "market_status": row[7] if len(row) > 7 else None,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def to_float(v: Any) -> float | None:
    """空串/None → None；数字/数字字符串 → float；无法解析 → None。

    实测坑：东财 Dryk="" 空串，直接 float("") 会炸，这里统一容忍。
    """
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 三格式解析（PowerShell / fetch / JSON）
# ---------------------------------------------------------------------------

_PS_COOKIE_RE = re.compile(
    r'New-Object\s+System\.Net\.Cookie\(\s*"([^"]+)"\s*,\s*"([^"]*)"\s*,'
)
_PS_KEY_RE = re.compile(r"validatekey=([A-Za-z0-9_\-]+)")
_PS_HOST_RE = re.compile(r'"([^"]*jywg\.18\.cn)"')
_PS_BODY_RE = re.compile(r'(-Body\b[^\r\n]*?["\']([^"\']+)["\']|Body\s+["\']([^"\']+)["\'])')

_FETCH_URL_RE = re.compile(r"fetch\(\s*['\"]([^'\"]+)['\"]")
_FETCH_BODY_RE = re.compile(r'["\']?body["\']?\s*:\s*["\']([^"\']*)["\']')
_FETCH_COOKIE_RE = re.compile(r'["\']?cookie["\']?\s*:\s*["\']([^"\']+)["\']', re.IGNORECASE)


def parse_paste(raw: str) -> Emsession | DirectSnapshot:
    """三格式自动识别解析。

    - PowerShell 报文（$session/Invoke-WebRequest）→ Emsession（含 Cookie 序列）
    - fetch 报文（fetch("...")）→ Emsession（validatekey + 可选 cookie 头；
      credentials:include 下浏览器自动带 Cookie，报文里无 Cookie 头属正常）
    - JSON 响应体（{"Status":...}）→ DirectSnapshot（直接入库模式，无凭据无拉取）
    - 其余 → EmsyncError（中文原因）

    解析失败只描述"缺什么"，绝不把报文原文带回错误信息（凭据防泄漏）。
    """
    text = (raw or "").strip()
    if not text:
        raise EmsyncError("粘贴内容为空：请先从 DevTools / PowerShell 复制报文")

    # 1) fetch 格式：以 fetch( 开头或含 "headers" 请求描述块
    if "fetch(" in text[:80] or text.startswith("fetch"):
        return _parse_fetch(text)

    # 2) PowerShell 格式
    if "$session" in text or "Invoke-WebRequest" in text or "WebRequestSession" in text:
        return _parse_powershell(text)

    # 3) JSON 响应体直通
    if text.startswith("{") and '"Status"' in text:
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, ValueError) as e:
            raise EmsyncError("JSON 解析失败：响应体不完整，请重新完整复制") from e
        if not isinstance(payload, dict):
            raise EmsyncError("JSON 格式不符：期望对象（{Status:...}）")
        return DirectSnapshot(snapshot=normalize(payload))

    raise EmsyncError(
        "无法识别粘贴格式：支持 PowerShell / fetch / JSON 响应体三种，"
        "请在 DevTools 网络面板对请求右键 Copy as fetch，或用 PowerShell 复制完整脚本"
    )


def _parse_powershell(text: str) -> Emsession:
    """PowerShell 报文：抓全部 Cookie("name","value",...) + validatekey + 域。"""
    cookies = _PS_COOKIE_RE.findall(text)
    key_match = _PS_KEY_RE.search(text)
    if not key_match:
        raise EmsyncError("未解析到 validatekey：请复制完整报文（含 URL query 参数）")
    if not cookies:
        raise EmsyncError(
            "未在报文中解析到 Cookie（$session.Cookies.Add 行）："
            "请完整复制 $session 初始化段（含全部 New-Object System.Net.Cookie 行）"
        )
    host = "jywg.18.cn"
    for domain in _PS_HOST_RE.findall(text):
        d = domain.lstrip(".")
        if "jywg.18.cn" in d:
            host = "jywg.18.cn"
            break
    cookie_header = "; ".join(f"{name}={value}" for name, value in cookies)
    return Emsession(validatekey=key_match.group(1), cookie_header=cookie_header, host=host)


def _parse_fetch(text: str) -> Emsession:
    """fetch 报文：抓 URL query validatekey + 可选 cookie 头 + body（moneyType）。

    注意：Chrome "Copy as fetch" 在 credentials:include 下浏览器自动带 Cookie，
    报文里通常没有 Cookie 头——允许解析通过，拉取 401 时由 fetch_positions
    提示用户改用 PowerShell 格式或连同 cookie 行一起复制。
    """
    url_match = _FETCH_URL_RE.search(text)
    if not url_match:
        raise EmsyncError("未解析到请求 URL：请对网络请求右键 Copy as fetch 后完整粘贴")
    url = url_match.group(1)
    key_match = _PS_KEY_RE.search(url)
    if not key_match:
        raise EmsyncError("未解析到 validatekey：URL query 中缺少该参数，请重新复制")
    cookie_header = ""
    cookie_match = _FETCH_COOKIE_RE.search(text)
    if cookie_match:
        cookie_header = cookie_match.group(1).strip()
    return Emsession(validatekey=key_match.group(1), cookie_header=cookie_header, host="jywg.18.cn")


# ---------------------------------------------------------------------------
# 拉取 + normalize
# ---------------------------------------------------------------------------

def fetch_positions(session: Emsession, timeout: float = 15.0) -> dict:
    """POST queryAssetAndPositionV1（§1.1 全要素），返回原始 JSON 响应。"""
    url = f"{EM_BASE}{QUERY_PATH}?validatekey={session.validatekey}"
    headers = {
        "User-Agent": _UA,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{EM_BASE}/Search/Position",
        "gw_reqtimestamp": str(int(time.time() * 1000)),
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    if session.cookie_header:
        headers["Cookie"] = session.cookie_header
    try:
        resp = requests.post(
            url, headers=headers, data="moneyType=RMB", timeout=timeout)
    except requests.RequestException as e:
        raise EmsyncError(f"网络请求失败（{type(e).__name__}），请检查网络后重试") from e

    if resp.status_code in (401, 403):
        if not session.cookie_header:
            raise EmsyncError(
                f"东财返回 {resp.status_code}：fetch 格式缺 Cookie，"
                "请改用 PowerShell 格式或连同 cookie 行一起复制"
            )
        raise EmsyncError(f"东财返回 {resp.status_code}：会话过期请重新复制")
    if resp.status_code != 200:
        raise EmsyncError(f"东财接口返回 HTTP {resp.status_code}，请稍后重试")
    try:
        payload = resp.json()
    except ValueError as e:
        raise EmsyncError("东财响应非 JSON（可能被风控页拦截），请稍后重新复制") from e
    if not isinstance(payload, dict):
        raise EmsyncError("东财响应格式异常，请重新复制")

    # Status/Errcode 非 0：通常为 validatekey 过期（时效 0.5-3h）
    if payload.get("Status") != 0 or payload.get("Errcode") != 0:
        errmsg = payload.get("Errmsg") or payload.get("Message") or ""
        raise EmsyncError(
            f"东财接口返回错误（Status={payload.get('Status')}, "
            f"Errcode={payload.get('Errcode')}，{str(errmsg)[:60]}）。会话过期请重新复制"
        )
    return payload


def normalize(resp: dict) -> Emsnapshot:
    """东财响应 → Emsnapshot：Data[0] 资产字段 + positions 映射表。

    容错：Dryk="" 空串统一 to_float；qty=0 或缺 code 的行跳过并计数。
    字段映射（§2）：Zqdm→stock_code、Zqmc→stock_name、Zqsl→qty、Cbjg→avg_cost、
    Zxjg→last_price、Zxsz→market_value、Ckyk→float_pnl、Ykbl→float_pnl_pct
    （已是小数，×100 展示）、Zqlx=='E'→etf、Kysl→avail_qty。
    """
    data_list = resp.get("Data") if isinstance(resp, dict) else None
    data = data_list[0] if isinstance(data_list, list) and data_list else {}

    positions: list[dict] = []
    skipped = 0
    for p in (data.get("positions") or []):
        if not isinstance(p, dict):
            skipped += 1
            continue
        code = str(p.get("Zqdm") or "").strip()
        qty = to_float(p.get("Zqsl"))
        if not code or not qty:   # qty=0 或空 → 跳过该行并计数
            skipped += 1
            continue
        positions.append({
            "stock_code": code,
            "stock_name": str(p.get("Zqmc") or "").strip(),
            "market": "cn",
            "qty": qty,
            "avg_cost": to_float(p.get("Cbjg")) or 0.0,
            "last_price": to_float(p.get("Zxjg")),
            "market_value": to_float(p.get("Zxsz")),
            "float_pnl": to_float(p.get("Ckyk")),
            "float_pnl_pct": to_float(p.get("Ykbl")),
            "today_pnl": to_float(p.get("Dryk")),
            "is_etf": str(p.get("Zqlx") or "") == "E",
            "avail_qty": to_float(p.get("Kysl")),
        })

    # 组合当日盈亏 = 各持仓行 Dryk（当日盈亏）求和；全空 → None
    today_pnls = [t for t in (pos.get("today_pnl") for pos in positions)
                  if t is not None]
    account = {
        "total_asset": to_float(data.get("Zzc")),
        "avail_cash": to_float(data.get("Kyzj")),
        "balance": to_float(data.get("Zjye")),
        "positions_market_value": to_float(data.get("totalSecMkval")),
        "today_pnl": round(sum(today_pnls), 2) if today_pnls else None,
    }
    return Emsnapshot(positions=positions, account=account, skipped_rows=skipped,
                      mode="session")


# ---------------------------------------------------------------------------
# diff 快照对比
# ---------------------------------------------------------------------------

_VALUE_TOL = 1e-6


def _row_matches(p: dict, row: dict) -> bool:
    """快照行与既有持仓行数值是否一致（qty/avg_cost 幂等判据）。"""
    return (
        abs(float(row.get("qty") or 0) - float(p["qty"])) <= _VALUE_TOL
        and abs(float(row.get("avg_cost") or 0) - float(p["avg_cost"])) <= _VALUE_TOL
    )


def diff_snapshot(new_positions: list[dict], existing_rows: list[dict]) -> dict:
    """快照 vs 既有持仓 → {added, updated, removed, unchanged}。

    - added：东财有、账本没有 → 新增行
    - updated：账本有且数值有变，或来源非 eastmoney（以券商为准，§3.7）→ 覆盖
    - removed：原 eastmoney 行从快照中消失 → 标记 closed（qty 清 0，不删行，保留历史）
    - unchanged：eastmoney 行且数值一致 → 幂等零写入
    """
    em_map = {p["stock_code"]: p for p in new_positions}
    existing_map = {row["stock_code"]: row for row in existing_rows
                    if (row.get("market") or "cn") == "cn"}

    added: list[dict] = []
    updated: list[dict] = []
    unchanged = 0
    for code, p in em_map.items():
        row = existing_map.get(code)
        if row is None:
            added.append(p)
        elif row.get("source") == "eastmoney" and _row_matches(p, row):
            unchanged += 1
        else:
            updated.append(p)

    removed: list[str] = []
    for code, row in existing_map.items():
        if row.get("source") == "eastmoney" and code not in em_map \
                and float(row.get("qty") or 0) > 0:
            removed.append(code)

    return {"added": added, "updated": updated, "removed": removed,
            "unchanged": unchanged}
