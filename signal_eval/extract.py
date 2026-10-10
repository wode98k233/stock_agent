"""信号提取：消息库 dashboard → signal 表（旁路任务，不阻塞 agent 主流程）。

- 扫描 `web_messages` 中 role='assistant' 且含 `@@DASHBOARD_START@@` 的消息
- 解析 dashboard JSON（decision_type: buy/sell/hold）
- 标的提取：① 6 位代码（全角括号兼容）→ ② 中文名查 stock_info 映射表
- 标的上下文：报告前最后一条 user > user 首条 > assistant 首条 > 消息本身
- 幂等：message_uuid UNIQUE，重复扫描自然跳过
- 实时入口：agent 回答落库后 `extract_async`（fire-and-forget）定向提取该条消息
"""
import json
import logging
import os
import re
import sqlite3
import threading

from utils.app_paths import get_db_path, get_market_data_db_path
from signal_eval.db import SIGNAL_EVAL_DB_PATH

logger = logging.getLogger("radar.signal_eval")

MSG_DB_PATH = get_db_path()
STOCK_INFO_DB_PATH = get_market_data_db_path()
SIGNAL_DB_PATH = SIGNAL_EVAL_DB_PATH

# confidence_level 文本 → 数值（legacy 兜底；现行 schema 直接输出 0-1 浮点，见 _parse_confidence）
_CONF_MAP = {"high": 0.8, "medium": 0.6, "low": 0.4}
_VALID_DECISIONS = ("buy", "sell", "hold")

_DASHBOARD_RE = re.compile(r"@@DASHBOARD_START@@(.*?)@@DASHBOARD_END@@", re.S)
# 中文连续段（不限长度）——滑动窗口不再按 {2,8} 切段，避免「中航光电」等名称跨正则切段被漏掉
_NAME_RE = re.compile(r"[\u4e00-\u9fa5]+")

# 大盘日评特征：scenario_tag 命中 + 标的类字段全空（symbol_signals/candidate_stocks/
# leading_stocks/hot_stocks），或含市场级字段（index_data/market_breadth）。
# 根因修复：80 条未识别里 50+ 条是"今日大盘总结/下周怎么看"类日评——顶层 decision
# 是对大盘/板块的判断而非具体标的，没有标的可补，不应堆进未识别清单。
_SCENARIO_PREFIX = "个股"  # scenario_tag 以此开头才视作个股仪表盘

_MARKET_DASH_KEYS = ("index_data", "market_breadth")
_SYMBOL_DASH_KEYS = ("symbol_signals", "candidate_stocks", "leading_stocks", "hot_stocks")

# stock_info 名称 → symbol_key 缓存（进程内）
_name_cache: dict[str, str | None] = {}
_name_of: dict[str, str] = {}  # symbol_key → name（落库用）

# ── stock_info 名称 ↔ 代码 预加载映射（进程内，按 DB 路径失效重载）──
# 实测 stock_info.code 是裸代码（'600519'/'002179'，market='SH'/'SZ'），
# 旧实现查 `code LIKE 's_%'` 永远命中 0 行 → 纯中文名提问的信号全部被悄悄丢弃。
# 此处一次性全表加载 name → 裸代码，避免逐名开连接的 N+1 慢查。
_name_map: dict[str, str] = {}          # 中文名（简称）→ 裸代码
_name_sorted: list[str] = []            # 名称按长度降序（长名优先，避免「平安」先于「平安银行」命中）
_name_map_path: str | None = None       # 已加载的 DB 路径（测试 monkeypatch 换库时自动重载）


def _code_to_symbol_key(code) -> str | None:
    """任意代码形态 → symbol_key（兼容裸代码 600519 / 前缀 sh600519），识别失败返回 None。"""
    if not code:
        return None
    s = str(code).strip()
    digits = re.sub(r"\D", "", s[2:] if s[:2].lower() in ("sh", "sz") else s)
    if len(digits) > 6:
        digits = digits[-6:]
    if len(digits) != 6:
        return None
    if digits.startswith(("6", "9", "5")):
        return f"sh{digits}"
    return f"sz{digits}"


def _load_name_map() -> dict[str, str]:
    """加载（或按路径失效重载）stock_info 中文名 → 裸代码 全量映射，并维护长名优先候选列表。"""
    global _name_map, _name_sorted, _name_map_path
    path = STOCK_INFO_DB_PATH
    if _name_map_path == path and _name_map:
        return _name_map
    _name_map = {}
    try:
        conn = sqlite3.connect(path)
        try:
            for code, name in conn.execute(
                "SELECT code, name FROM stock_info WHERE name IS NOT NULL AND name != ''"
            ):
                _name_map.setdefault(name, code)
        finally:
            conn.close()
    except Exception:
        _name_map = {}
    _name_sorted = sorted((n for n in _name_map if len(n) >= 2), key=len, reverse=True)
    _name_map_path = path
    return _name_map


def _stock_name_to_symbol(name: str) -> str | None:
    """中文名 → 'sh600519'（查 market_data.db::stock_info 名称映射，代码为裸码/带前缀均可）。"""
    if name in _name_cache:
        return _name_cache[name]
    code = _load_name_map().get(name)
    sym = _code_to_symbol_key(code) if code else None
    if sym:
        _name_of[sym] = name
    _name_cache[name] = sym
    return sym


def parse_dashboard(content: str) -> dict | None:
    """从消息内容提取 dashboard JSON，解析失败返回 None。"""
    m = _DASHBOARD_RE.search(content or "")
    if not m:
        return None
    try:
        return json.loads(m.group(1).strip())
    except (json.JSONDecodeError, TypeError):
        return None


def extract_symbol(text: str) -> str | None:
    """从文本提取标的代码 → 'sh600519' / 'sz300442' / 'sh510880'（ETF）。

    ① 6 位数字代码（复用记忆系统 `_extract_stock_codes_from_text`，
       天然兼容全角括号（300442）内的代码）
    ② 文本显式含 "ETF" 字样 → 优先走 ETF 通道：用户问「机器人ETF」指的是基金
       而非「机器人」（300024 个股简称）——通用词个股简称与 ETF 简称大量同名，
       必须先于个股名称匹配。
    ③ 无代码 → 预加载名称映射全量匹配（长名优先；直接子串查找，修复名称
       跨正则切段被漏掉的遗漏——如「中航光电」此前被 {2,8} 切段拆散）
    ④ 个股未命中 → ETF 别名词典（红利ETF/消费ETF 等简称 → 裸代码 → symbol_key）
    """
    from memory.metadata import _extract_stock_codes_from_text
    codes = _extract_stock_codes_from_text(text or "")
    if codes:
        return _code_to_symbol_key(codes[0])
    text = text or ""
    # 文本显式提到 ETF → 先走 ETF 通道（全量目录关键词匹配已灌库，覆盖 1600+ 只）
    if "ETF" in text.upper():
        key = _extract_etf_from_text(text)
        if key:
            return key
    # 中文名 → 代码：全量名称表直接子串命中（长名优先，避免「平安」先于「平安银行」被选）
    _load_name_map()
    for name in _name_sorted:
        if len(name) >= 2 and name in text:
            sym = _stock_name_to_symbol(name)
            if sym:
                return sym
    # ETF 简称兜底（无 ETF 字样但用了「红利ETF」等别名的问法）
    return _extract_etf_from_text(text)


def _extract_etf_from_text(text: str) -> str | None:
    """ETF 通道：别名词典滑窗匹配（含全量目录名称匹配）。命中返回 symbol_key。"""
    from signal_eval.symbols import resolve_etf_alias
    for seg in _NAME_RE.findall(text or ""):
        seg = seg[:48]  # 长文本只扫前 48 字，控制窗口开销
        n = len(seg)
        for length in range(min(7, n), 1, -1):
            for i in range(0, n - length + 1):
                key = resolve_etf_alias(seg[i:i + length])
                if key:
                    _etf_name_of(key)
                    return key
    return None


def _etf_name_of(symbol_key: str) -> None:
    """ETF symbol_key → 名称回填 _name_of（落库用；查不到则用代码）。"""
    if symbol_key in _name_of:
        return
    try:
        conn = sqlite3.connect(STOCK_INFO_DB_PATH)
        try:
            row = conn.execute(
                "SELECT name FROM stock_info WHERE code=? AND market='ETF' LIMIT 1",
                (symbol_key[2:],),
            ).fetchone()
        finally:
            conn.close()
        _name_of[symbol_key] = row[0] if row else symbol_key[2:]
    except Exception:
        _name_of[symbol_key] = symbol_key[2:]


def _first_msg(conn: sqlite3.Connection, dialog_uuid: str, role: str) -> str:
    row = conn.execute(
        "SELECT content FROM web_messages WHERE dialog_uuid=? AND role=? ORDER BY id LIMIT 1",
        (dialog_uuid, role),
    ).fetchone()
    return (row[0] if row else "") or ""


def _parse_conf(value) -> float | None:
    """任意 confidence 值 → 0-1 或 None（数值直存/文本兜底/缺失不报错）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    if value:
        text = str(value).strip().lower()
        if text in _CONF_MAP:
            return _CONF_MAP[text]
        try:
            return max(0.0, min(1.0, float(text)))
        except ValueError:
            return None
    return None


def _parse_confidence(dash: dict) -> float | None:
    """dashboard JSON → confidence（0-1 float 或 None）。

    A2 修复（概要设计 §0.3-M1）：LLM 实际输出数值型 confidence_level（如 0.55/0.65），
    旧实现只走 _CONF_MAP 文本映射导致全量 NULL。现行规则见 _parse_conf：
    数值直存并 clamp；数值字符串转 float；文本 legacy；缺失/异常 None（不报错）。
    """
    return _parse_conf(dash.get("confidence_level"))


def _parse_confidence_basis(dash: dict) -> str | None:
    """dashboard JSON → confidence_basis（JSON 数组字符串，≤3 条；无有效依据返回 None）。

    非法类型（非 list）与空数组均返回 None——解析失败不报错（AC-6）。
    """
    basis = dash.get("confidence_basis")
    if not isinstance(basis, list):
        return None
    items = [str(x).strip() for x in basis if str(x).strip()]
    if not items:
        return None
    return json.dumps(items[:3], ensure_ascii=False)


def _resolve_symbol_key(code, name: str, ctx: str) -> str | None:
    """解析 symbol_key：code（6位，兼容 sh/sz 前缀）→ name（查 stock_info）→ ctx 兜底。"""
    if code:
        sym = _code_to_symbol_key(code)
        if sym:
            return sym
    if name:
        sym = _stock_name_to_symbol(str(name).strip())
        if sym:
            return sym
    return extract_symbol(ctx)


def _signals_items(dash: dict | None, ctx: str) -> list[dict]:
    """从 dashboard 生成待入库信号列表。

    优先 `symbol_signals` 数组（组合仪表盘多标的）：逐项解析
    code/name/decision/confidence/condition；回退旧逻辑（顶层 decision_type
    + extract_symbol(ctx) 单标的）。

    遗漏修复点：
    - 不再要求顶层 decision_type 合法 —— 只要任一 symbol_signals 条目自带合法
      decision 即处理（修复"组合仪表盘无顶层决策 → 整条消息被丢弃"）。
    - 标的无法解析时不再静默丢弃，产出 symbol_key=None 的条目（入库后进
      "未识别"清单，可人工补标或标记大盘类，而非完全消失）。
    - 大盘日评（含 index_data/market_breadth 等市场级字段且无标的候选）识别不出
      标的时直接自动标 is_market_wide=1：这类决策本来就是评大盘/板块，没有
      标的可补，不再堆进未识别清单。

    item: {symbol_key(可为 None), symbol_name, decision, confidence, basis, verdict,
           sentiment, market_wide}
    """
    if not dash:
        return []
    top_decision = dash.get("decision_type")
    sub = dash.get("symbol_signals")
    if isinstance(sub, list) and sub:
        top_conf = _parse_confidence(dash)
        top_basis = _parse_confidence_basis(dash)
        top_verdict = dash.get("core_verdict")
        sentiment = dash.get("sentiment_score")
        items = []
        for s in sub:
            if not isinstance(s, dict):
                continue
            decision = s.get("decision") or top_decision
            if decision not in _VALID_DECISIONS:
                continue
            symbol = _resolve_symbol_key(s.get("code"), s.get("name"), ctx)
            conf = _parse_conf(s.get("confidence")) if s.get("confidence") is not None else top_conf
            basis = _parse_confidence_basis(
                {"confidence_basis": s.get("confidence_basis") or dash.get("confidence_basis")})
            items.append({
                "symbol_key": symbol,   # 可能为 None（未识别条目，照样入库进未识别清单）
                "symbol_name": (_name_of.get(symbol) or s.get("name")) if symbol else s.get("name"),
                "decision": decision,
                "confidence": conf,
                "basis": basis,
                "verdict": s.get("condition") or s.get("note") or top_verdict,
                "sentiment": sentiment,
                "market_wide": False,   # 组合仪表盘条目带名称/代码，属可补标标的
            })
        if items:
            return items
    if top_decision in _VALID_DECISIONS:
        symbol = extract_symbol(ctx)
        market_dash = _is_market_wide_dash(dash)
        return [{
            "symbol_key": symbol,   # 可能为 None（未识别，进未识别清单而非丢弃）
            "symbol_name": _name_of.get(symbol) if symbol else None,
            "decision": top_decision,
            "confidence": _parse_confidence(dash),
            "basis": _parse_confidence_basis(dash),
            "verdict": dash.get("core_verdict"),
            "sentiment": dash.get("sentiment_score"),
            "market_wide": symbol is None and market_dash,
        }]
    return []


def _is_market_wide_dash(dash: dict) -> bool:
    """大盘日评判定：无任何标的类字段（symbol_signals/candidate_stocks/leading_stocks/
    hot_stocks），且 dashboard 显式声明非个股（scenario_tag 不以"个股"开头）或含市场级
    特征字段（index_data/market_breadth）。

    这类 dashboard 的顶层 decision 是对大盘/板块的判断（"今天大盘总结""下周怎么看"），
    没有具体标的可补标，自动归档大盘类（is_market_wide=1），不进未识别清单。
    """
    if any(dash.get(k) for k in _SYMBOL_DASH_KEYS):
        return False
    if any(dash.get(k) for k in _MARKET_DASH_KEYS):
        return True
    scenario = str(dash.get("scenario_tag") or "")
    return bool(scenario) and not scenario.startswith(_SCENARIO_PREFIX)


def _insert_signal(sig_conn: sqlite3.Connection, row, it: dict) -> int:
    """写入单条信号，返回新增行数（幂等）。

    - symbol_key 非空：INSERT OR IGNORE（UNIQUE(message_uuid, symbol_key)）+ A2 存量
      confidence/basis 回填（WHERE confidence IS NULL 守卫不覆盖人工标注）；
      落库成功后清理同 message 的 NULL 残留行（旧识别逻辑失败留下的，其信息
      已被识别行覆盖，不清理会永远挂在未识别清单）。
    - symbol_key 为空（未识别标的）：UNIQUE 对 NULL 不生效，需先查重再插入，
      保证重扫/重启都不产生重复行；大盘日评类（it["market_wide"]）直接标
      is_market_wide=1 归档，其余进 /api/signal-eval/unrecognized 清单。
    """
    msg_id = str(row["id"])
    if it["symbol_key"]:
        cur = sig_conn.execute(
            "INSERT OR IGNORE INTO signal"
            "(dialog_uuid,message_uuid,symbol_key,symbol_name,decision,confidence,sentiment,core_verdict,signal_time,confidence_basis) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (row["dialog_uuid"], msg_id, it["symbol_key"], it["symbol_name"],
             it["decision"], it["confidence"], it["sentiment"], it["verdict"],
             row["created_at"], it["basis"]),
        )
        if cur.rowcount == 0 and (it["confidence"] is not None or it["basis"] is not None):
            # A2 存量回填：行已存在（旧数据 confidence/basis 为 NULL）时仅补两列，
            # 按 (message_uuid, symbol_key) 定位；WHERE 守卫保证幂等不覆盖人工标注。
            sig_conn.execute(
                "UPDATE signal SET confidence=?, confidence_basis=? "
                "WHERE message_uuid=? AND symbol_key=? AND confidence IS NULL",
                (it["confidence"], it["basis"], msg_id, it["symbol_key"]),
            )
        # 清理同 message 的 NULL 残留行：识别行已在库，残留行仅造成未识别清单噪音。
        # 只删 symbol_name 为空的行——symbol_name 非空说明有人工标注痕迹，保留。
        sig_conn.execute(
            "DELETE FROM signal WHERE message_uuid=? AND symbol_key IS NULL "
            "AND (symbol_name IS NULL OR symbol_name='')",
            (msg_id,),
        )
        return cur.rowcount
    # 未识别标的：查重后插入（持久幂等，跨重启不重复）
    if sig_conn.execute(
        "SELECT 1 FROM signal WHERE message_uuid=? AND symbol_key IS NULL LIMIT 1",
        (msg_id,),
    ).fetchone():
        return 0
    market_wide = 1 if it.get("market_wide") else 0
    cur = sig_conn.execute(
        "INSERT INTO signal"
        "(dialog_uuid,message_uuid,symbol_key,symbol_name,decision,confidence,sentiment,core_verdict,signal_time,confidence_basis,is_market_wide) "
        "VALUES(?,?,NULL,?,?,?,?,?,?,?,?)",
        (row["dialog_uuid"], msg_id, it["symbol_name"], it["decision"], it["confidence"],
         it["sentiment"], it["verdict"], row["created_at"], it["basis"], market_wide),
    )
    return cur.rowcount


def extract_one(msg_conn: sqlite3.Connection, sig_conn: sqlite3.Connection, row) -> int:
    """提取单条 assistant 消息 → signal 表，返回新增行数（多标的可 >1）。

    row 需含 id/dialog_uuid/content/created_at（sqlite3.Row）。
    不 commit（由调用方批量提交或单条提交）。
    """
    dash = parse_dashboard(row["content"])
    if not dash:
        return 0
    # 标的上下文：本报告前最后一条 user（提问通常含标的）> user 首条 > assistant 首条 > 消息本身
    ctx = (_last_user_msg_before(msg_conn, row["dialog_uuid"], row["id"])
           or _first_msg(msg_conn, row["dialog_uuid"], "user")
           or _first_msg(msg_conn, row["dialog_uuid"], "assistant")
           or row["content"])
    added = 0
    for it in _signals_items(dash, ctx):
        added += _insert_signal(sig_conn, row, it)
    return added


def _last_user_msg_before(conn: sqlite3.Connection, dialog_uuid: str, before_id: int) -> str:
    """对话内先于 before_id（assistant 报告 id）的最后一条 user 消息。"""
    try:
        row = conn.execute(
            "SELECT content FROM web_messages "
            "WHERE dialog_uuid=? AND role='user' AND id<? ORDER BY id DESC LIMIT 1",
            (dialog_uuid, before_id),
        ).fetchone()
        return (row[0] if row else "") or ""
    except Exception:
        return ""


def _build_first_msg_maps(msg_conn: sqlite3.Connection) -> tuple[dict[str, str], dict[str, str], dict[int, str]]:
    """一次查询构建标的上下文映射（ORDER BY id 升序单遍扫描）。

    返回：
    - user_map: dialog_uuid → 首条 user（兜底）
    - asst_map: dialog_uuid → 首条 assistant（兜底）
    - last_user_map: assistant 消息 id → 先于它的最后一条 user（二期口径，主上下文）

    替代逐消息两条 `_first_msg` 查询的 N+1，抹掉重扫的主要 I/O 开销。
    """
    user_map: dict[str, str] = {}
    asst_map: dict[str, str] = {}
    last_user_map: dict[int, str] = {}
    last_user: dict[str, str] = {}
    try:
        for dlg, role, mid, content in msg_conn.execute(
            "SELECT dialog_uuid, role, id, content FROM web_messages "
            "WHERE role IN ('user','assistant') ORDER BY id"
        ):
            text = content or ""
            if role == "user":
                user_map.setdefault(dlg, text)
                last_user[dlg] = text
            elif role == "assistant":
                asst_map.setdefault(dlg, text)
                if dlg in last_user:
                    last_user_map[mid] = last_user[dlg]
    except Exception:
        pass  # 索引/表缺失等异常安全降级为空映射（extract_one 兜底路径仍可用）
    return user_map, asst_map, last_user_map


def _extract_items_from_row(row, user_map: dict[str, str], asst_map: dict[str, str],
                            last_user_map: dict[int, str]) -> list[dict]:
    """仅做内存解析（dashboard 解析 + 标的识别），不碰 DB 写 —— 并行阶段安全复用的纯函数。"""
    dash = parse_dashboard(row["content"])
    if not dash:
        return []
    ctx = (last_user_map.get(row["id"])
           or user_map.get(row["dialog_uuid"])
           or asst_map.get(row["dialog_uuid"])
           or row["content"])
    return _signals_items(dash, ctx)


def scan_messages() -> int:
    """增量扫描消息库，返回新增信号数（幂等）。

    - 预加载 stock_info 名称映射 + 一次查询构建各对话首条上下文，抹掉逐消息 N+1 慢查；
    - 消息解析（纯内存，无 DB 读）用线程池并行加速；
    - 信号写入集中在主线程单连接串行执行（INSERT OR IGNORE / 未识别查重），保证 sqlite 线程安全。

    全量扫描幂等。不做 last_id 增量：assistant 消息先建行后更新内容
    （落库时 id 早于上次扫描点），按 id 增量会漏掉更新进来的 dashboard；
    实时增量由 extract_async 定向提取兜住。
    """
    msg_conn = sqlite3.connect(MSG_DB_PATH)
    msg_conn.row_factory = sqlite3.Row
    from signal_eval.db import init_db
    init_db(SIGNAL_DB_PATH)
    sig_conn = sqlite3.connect(SIGNAL_DB_PATH)
    try:
        rows = msg_conn.execute(
            "SELECT id, dialog_uuid, content, created_at FROM web_messages "
            "WHERE role='assistant' AND content LIKE '%@@DASHBOARD_START@@%' ORDER BY id"
        ).fetchall()
        if not rows:
            return 0
        _load_name_map()                              # 预热：中文名 → 裸代码（一次全表读）
        from signal_eval.symbols import warmup_etf_rows
        warmup_etf_rows()                             # 预热：ETF 基金名行（一次全表读）
        user_map, asst_map, last_user_map = _build_first_msg_maps(msg_conn)

        # 并行解析（只读进程内映射，线程安全）
        import concurrent.futures
        resolved: dict[int, list[dict]] = {}
        workers = min(8, max(1, os.cpu_count() or 1), len(rows))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            fut_map = {ex.submit(_extract_items_from_row, r, user_map, asst_map, last_user_map): r["id"] for r in rows}
            for fut in concurrent.futures.as_completed(fut_map):
                rid = fut_map[fut]
                try:
                    resolved[rid] = fut.result()
                except Exception:
                    resolved[rid] = []

        # 串行写库（单连接）
        added = 0
        for r in rows:
            for it in resolved.get(r["id"], []):
                added += _insert_signal(sig_conn, r, it)
        sig_conn.commit()
        return added
    finally:
        msg_conn.close()
        sig_conn.close()


def extract_by_message_uuid(message_uuid: str) -> bool:
    """按 message_uuid 定向提取单条消息（实时 hook / 补录用）。

    返回是否新增；消息不存在 / 无 dashboard / 解析失败均安全返回 False。
    """
    msg_conn = sqlite3.connect(MSG_DB_PATH)
    msg_conn.row_factory = sqlite3.Row
    try:
        row = msg_conn.execute(
            "SELECT id, dialog_uuid, content, created_at FROM web_messages "
            "WHERE message_uuid=? AND role='assistant'",
            (message_uuid,),
        ).fetchone()
        if row is None:
            return False
        from signal_eval.db import init_db
        init_db(SIGNAL_DB_PATH)
        sig_conn = sqlite3.connect(SIGNAL_DB_PATH)
        try:
            added = extract_one(msg_conn, sig_conn, row)
            sig_conn.commit()
            return added > 0
        finally:
            sig_conn.close()
    finally:
        msg_conn.close()


def extract_async(message_uuid: str) -> None:
    """fire-and-forget 入口：后台线程定向提取，异常全吞（绝不影响 agent 主流程）。"""
    def _run():
        try:
            extract_by_message_uuid(message_uuid)
        except Exception:  # noqa: BLE001
            logger.debug("signal 提取 hook 失败（忽略）: %s", message_uuid, exc_info=True)

    threading.Thread(target=_run, daemon=True, name="signal-extract-hook").start()


def backlog_cleanup() -> dict:
    """存量未识别信号清理（一次性维护，幂等）。

    两步（顺序不可颠倒）：
    1. 删除同 message 已存在识别行的 NULL 残留行——旧识别逻辑失败留下的垃圾行，
       其信息已被识别行覆盖，只造成未识别清单噪音（不删 symbol_name/人工标注行）。
    2. 剩余 NULL 行按 dashboard 日评特征（index_data/market_breadth 且无标的候选）
       重判，命中则标 is_market_wide=1 归档大盘类。

    返回 {removed, archived}。
    """
    msg_conn = sqlite3.connect(MSG_DB_PATH)
    msg_conn.row_factory = sqlite3.Row
    sig_conn = sqlite3.connect(SIGNAL_DB_PATH)
    sig_conn.row_factory = sqlite3.Row
    try:
        # 残留 NULL 行（同 message 已有识别行）：symbol_name 为空即可删——其信息已被
        # 识别行覆盖；不设 is_market_wide/confidence 门槛（自动归档/含 confidence 的
        # 残留行同样只是清单噪音，且 symbol_name 为空证明从未被人工补标）。
        removed = sig_conn.execute(
            "DELETE FROM signal WHERE symbol_key IS NULL "
            "AND (symbol_name IS NULL OR symbol_name='') AND EXISTS ("
            "  SELECT 1 FROM signal s2 WHERE s2.message_uuid=signal.message_uuid"
            "  AND s2.symbol_key IS NOT NULL)"
        ).rowcount
        archived = 0
        rows = sig_conn.execute(
            "SELECT id, message_uuid FROM signal WHERE symbol_key IS NULL AND is_market_wide=0"
        ).fetchall()
        for r in rows:
            m = msg_conn.execute(
                "SELECT content FROM web_messages WHERE id=?", (r["message_uuid"],)
            ).fetchone()
            if not m:
                continue
            dash = parse_dashboard(m["content"])
            if dash and _is_market_wide_dash(dash):
                archived += sig_conn.execute(
                    "UPDATE signal SET is_market_wide=1 WHERE id=? AND symbol_key IS NULL",
                    (r["id"],),
                ).rowcount
        sig_conn.commit()
        logger.info("未识别存量清理：删除残留 %s 行，归档大盘类 %s 行", removed, archived)
        return {"removed": removed, "archived": archived}
    finally:
        msg_conn.close()
        sig_conn.close()
