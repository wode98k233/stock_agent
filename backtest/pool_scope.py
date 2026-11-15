"""标的池范围解析：把「对话里的范围说法」落成一份可回测的代码清单。

为什么单独一个模块
------------------
此前范围只有两个预设（`POOL:default` / `POOL:smoke`）或一串手写代码。用户
2026-10-24 明确要求「范围不能就那么几个选项，要能聊、能定」——所以范围必须
能从自然语言解析成**具体清单**，并且在确认卡上如实显示「多少只、数据快照哪天」。

支持的 spec（`POOL:` 前缀，可带 `|limit=N|kind=stock|etf`）
------------------------------------------------------------
- `POOL:default` / `POOL:smoke`：内置池
- `POOL:index:000300|000905`：指数成分股（本地快照，非实时）
- `POOL:watchlist`：我的自选
- `POOL:portfolio`：我的持仓
- `POOL:union`：自选 ∪ 持仓
- `POOL:board:医药`：行业/概念板块（按板块名匹配，可能命中多个板块）
- `POOL:codes:600519,510300`：明确点名的代码
- 选项：`limit`（从清单里**均匀抽** N 只，保留顺序可复现）、`kind`（只看个股/ETF）

口径
----
- **数据缺失就报错，不编数**：本地没快照/自选为空 → 抛 `ValueError`，由调用方
  在确认卡/起跑处如实告知。
- **快照不是实时**：指数成分与板块成员都带 `as_of_date`，标签里必须写出来。
- 纯 DB 读取，不做网络请求（卡住首屏的就是网络）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# ⚠️ 红线（2026-11-01）：**名称→代码的映射是数据，不是代码**。
# 指数别名（沪深300/沪深三百/hs300 → 000300、中证500/zz500 → 000905 …）统一存
# `market_data.db.symbol_alias`（kind='index'，数据源 `data/symbol_aliases.json`，
# 灌库 `scripts/ops/seed_symbol_aliases.py --apply`），本文件只查询
# （`backtest/symbol_alias.py`）。不要再往本文件里加任何 `{'沪深300': '000300'}`
# 之类的字典或 `'沪深300' in text` 的判定（守卫：
# test/unit/test_no_hardcoded_symbol_aliases.py）。

_BOARD_RE = re.compile(r'([\u4e00-\u9fa5A-Za-z0-9]{2,10})(?:板块|行业|概念)')
_BOARD_STOP = {'这个', '那个', '哪个', '哪些', '什么', '这些', '那些', '该', '大', '小'}
_LIMIT_RE = re.compile(r'(?:跑|抽|选|取|前|找到|凑|要|来)\s*(\d{1,4})\s*只')
_TOTAL_LIMIT_RE = re.compile(r'总数(?:达到|到|为|约)?\s*(\d{1,4})')
_CODE_RE = re.compile(r'(?<!\d)(\d{6})(?!\d)')

# ── 中文标的解析（真机 2026-10-25）────────────────────────────
# 用户原话点名了「CPO（通信ETF）、半导体ETF、科创50ETF、人工智能ETF、剑桥科技、
# 易中天这三个光模块龙头」，旧实现只认 6 位代码/指数/自选，认不出名字后**静默**
# 落成 `POOL:mixed` —— 跑出来是「沪深300 + ETF 315 只」，与用户要的科技标的完全无关。
#
# ⚠️ 红线（2026-11-01）：**名称→代码的映射是数据，不是代码**。
# 别名/俗称（指数、ETF 简称、个股简称、统称）统一存 `market_data.db.symbol_alias`
# （由 `data/symbol_aliases.json` + `scripts/ops/seed_symbol_aliases.py` 维护），
# 这里只查询（`backtest/symbol_alias.py`）。
# 不要再往本文件里加任何 `{'通信etf': '515880'}` / `{'沪深300': '000300'}` 之类的
# 字典，也不要写 `'沪深300' in text ... target = '000300'` 这种旁路映射。

# 这些词不是标的（概念词/动作词/泛称），不参与名字解析
_TARGET_STOP = {
    '个股', '股票', 'etf', '基金', '标的', '龙头', '板块', '策略', '回测', '混合',
    '科技', '超跌', '反弹', '深v', '区间', '收盘', '开盘', '持有', '买入', '卖出',
    '光模块', '算力', '芯片', '半导体', '人工智能', '通信', '医药', '新能源',
    '机器人', '军工', '消费', '金融', '地产', '电力', '有色', '煤炭', '汽车',
}
# 含这些子串的 token 也不是标的（「科技相关的个股」「明天的买点」）
_TARGET_SKIP_SUBSTR = (
    '相关', '个股', '股票', '单只', '单票', '只看', '只做', '策略', '条件',
    '区间', '回测', '验证', '研究', '买入', '卖出', '持有', '开盘', '收盘',
    '反弹', '超跌', '板块', '混合', '标的',
)
_CLAUSE_SPLIT_RE = re.compile(
    r'[、，,。；;！!?？\s]+|以及|还有|比如|例如|包括|等等|等|和|与|及|跟'
    r'|只做|只看|只买|就做|就买|这三个|这一个|这些|那些|之类')
_CHINESE_NAME_RE = re.compile(r'^[\u4e00-\u9fa5A-Za-z0-9]{2,12}$')

# 「标的清单句」的引导词：只有这些词后面的一段才是**点名标的**的语境。
# 没有这个约束，原话里的「我问你」「收获如何」都会被当成没认出的股票名写进确认卡。
_TARGET_INTRO_RE = re.compile(
    r'(?:标的(?:用|是|选|就)?|比如|例如|包括|成分股|个股|股票|ETF|基金)'
    r'([^。；;\n]{0,200})')


def _split_options(raw: Any) -> Tuple[str, Dict[str, str]]:
    body, opts = str(raw or '').strip(), {}
    if '|' in body:
        parts = body.split('|')
        body = parts[0].strip()
        for part in parts[1:]:
            key, _, val = part.partition('=')
            if key.strip():
                opts[key.strip().lower()] = val.strip()
    return body, opts


def normalize_index_target(target: Any) -> Optional[str]:
    """指数别名 → 代码（000300 / 000905）；不认识的 6 位码原样返回，其余 None。

    别名表在库里（kind='index'），本函数只查；库里没有 `沪深300` 就没这回事，
    绝不在代码里兜底猜。
    """
    from backtest.symbol_alias import lookup_one

    text = str(target or '').strip()
    if not text:
        return None
    code = lookup_one(text, kind='index')
    if code:
        return code
    return text if re.fullmatch(r'\d{6}', text) else None


def _index_alias_in_text(text: str) -> Optional[Tuple[str, str]]:
    """原话里命中的指数别名 → (别名, 代码)；没命中 None。

    只认库里 kind='index' 的别名（长别名优先、等长按数据文件行序）。
    旧实现把 `沪深300 / 沪深三百 / hs300` 写死在 elif 链里，改一个指数要改两处代码。

    子串要再筛一道：`跑沪深300ETF` 里也含「沪深300」，但用户点名的是那只 ETF，
    不能当成分股范围（真机 2026-10-25 的「静默换标的」就是这类）。
    规则：命中的指数名若被原话里某个**更长的 ETF 简称**包含，跳过它。
    """
    from backtest.symbol_alias import find_aliases_in_text, list_aliases

    raw = str(text or '').lower()
    if not raw.strip():
        return None
    etf_names = [name.lower() for name, _ in list_aliases('etf')]
    for alias, code in find_aliases_in_text(text, kind='index'):
        name = alias.lower()
        if any(len(etf) > len(name) and name in etf and etf in raw for etf in etf_names):
            continue
        return alias, code
    return None


def _index_alias_code(token: str) -> Optional[str]:
    """token 若是指数别名 → 指数代码（仅 kind='index'）。

    范围解析里指数名按**指数**处理（成分股），不是同名 ETF：库里 `沪深300` 有两条
    （index→000300 / etf→510300），这里只认 index，否则「沪深300和中证500」会被
    解析成两只 ETF 的代码池（历史行为是走指数范围）。
    """
    from backtest.symbol_alias import lookup_one

    return lookup_one(token, kind='index')


def _index_name_only(code: str, name: str) -> bool:
    """`stock_info` 里这条「名字→代码」是不是指数被当股票灌进来的（如 000300 沪深300）。

    P1（2026-11-01）：别名库不可用时，`沪深300` 会从 `stock_info` 命到 `000300`
    （本机 stock_info 里确实有指数行），旧实现就把它当个股落成 `POOL:codes:000300`
    —— 静默换标的。这里用**数据**判定：`index_info` 里该码的名字与命中的名字一致
    ⇒ 这是指数，不是可交易标的。

    为什么不直接「命中 index_info 就拒」：`000001` 既在上证指数也在平安银行
    （实测唯一重叠码），指数表命中就拒会误杀平安银行；只有**名字也一致**才算指数。
    """
    try:
        from utils.cache.market_data_db import get_index_name
        idx_name = get_index_name(str(code or '').strip())
        return bool(idx_name) and str(idx_name).strip() == str(name or '').strip()
    except Exception:  # noqa: BLE001 - 元数据不可用不阻塞（宁可放行，也不误拒个股）
        return False


def _index_only_code(code: str) -> bool:
    """裸码是否「只能当指数」（000300 沪深300 / 399006 创业板指），不能进代码池。

    与 `utils.cache.market_data_db.resolve_symbol_market` 同口径，但必须带 name_hint：
    本机 `stock_info` 里也有 000300（指数被当股票灌过），不带 hint 会被判成 STOCK。
    """
    code = str(code or '').strip()
    if not re.fullmatch(r'\d{6}', code):
        return False
    try:
        from utils.cache.market_data_db import get_index_name, get_stock_info, resolve_symbol_market
        if not get_index_name(code):
            return False
        info = get_stock_info(code) or {}
        return resolve_symbol_market(code, name_hint=str(info.get('name') or '')) == 'INDEX'
    except Exception:  # noqa: BLE001
        return False


def _reject_index_codes(codes: List[str], where: str) -> None:
    """代码池里出现指数码 → 直接拒绝（指数不可直接交易，静默当个股回测是最坏结果）。"""
    idx = [c for c in codes if _index_only_code(c)]
    if not idx:
        return
    raise ValueError(
        f'{where} 里不能写指数代码 {"、".join(idx)}：指数不能当交易标的。'
        f'要跑成分股请用 POOL:index:{idx[0]}；要跑对应 ETF 请写 ETF 代码（如 510300）')


def build_spec(source: str, target: str = '', limit: Optional[int] = None,
               kind: str = 'all') -> str:
    """拼回规范 spec 字符串（router/调用方共用，格式只有这一处定义）。"""
    src = str(source or '').strip().lower()
    if src == 'default':
        body = 'POOL:default'
    elif src == 'smoke':
        body = 'POOL:smoke'
    elif src == 'index':
        code = normalize_index_target(target)
        if not code:
            raise ValueError(f'不认识的指数：{target}')
        body = 'POOL:index:' + code
    elif src in ('watchlist', 'portfolio', 'union', 'mixed'):
        body = 'POOL:' + src
    elif src == 'board':
        name = str(target or '').strip()
        if not name:
            raise ValueError('板块名不能为空')
        body = 'POOL:board:' + name
    elif src == 'codes':
        codes = _dedupe_codes(_CODE_RE.findall(str(target or '')))
        if not codes:
            raise ValueError(f'没有合法代码：{target}')
        _reject_index_codes(codes, '代码池')
        body = 'POOL:codes:' + ','.join(codes)
    else:
        raise ValueError(f'不认识的范围来源：{source}')
    opts = []
    if limit:
        opts.append('limit=' + str(int(limit)))
    if str(kind or '').lower() in ('stock', 'etf'):
        opts.append('kind=' + str(kind).lower())
    return body + ('|' + '|'.join(opts) if opts else '')


def is_scope_spec(raw: Any) -> bool:
    """是否是本模块新增的范围 spec（用于 pool_first 分流，老 spec 行为不变）。"""
    body, _ = _split_options(raw)
    up = body.upper()
    return bool(
        up.startswith('POOL:INDEX:') or up.startswith('POOL:BOARD:')
        or up.startswith('POOL:CODES:')
        or up in ('POOL:WATCHLIST', 'POOL:PORTFOLIO', 'POOL:UNION', 'POOL:MIXED')
        or '|' in str(raw or ''))          # 带 limit/kind 的默认池/烟测池也走这里


def _lookup_name(name: str) -> Tuple[str, str]:
    """中文名 → (6 位代码, 库里的规范名)；查不到 / ETF 简称有歧义 → ('', '')。

    只认 `length(code)=6` 的裸码行（`stock_info` 里还有 `sh603083` 这类带前缀的
    重复行，不排除会拿到前缀码）。ETF 库名带管理人后缀（「通信ETF国泰」），
    所以先走别名表，再按**唯一前缀**兜底。
    """
    text = str(name or '').strip()
    if not text:
        return '', ''
    try:
        from utils.cache.market_data_db import get_market_data_db

        with get_market_data_db() as conn:
            row = conn.execute(
                'SELECT code, name FROM stock_info WHERE name = ? AND length(code) = 6 '
                'ORDER BY CASE market WHEN ? THEN 0 WHEN ? THEN 1 ELSE 2 END LIMIT 1',
                (text, 'SH', 'SZ')).fetchone()
            if row:
                return str(row[0]), str(row[1])
            rows = conn.execute(
                "SELECT code, name FROM stock_info WHERE market = 'ETF' "
                'AND name LIKE ? AND length(code) = 6', (text + '%',)).fetchall()
        if len(rows) == 1:                      # 有歧义就不猜，交给用户补代码
            return str(rows[0][0]), str(rows[0][1])
    except Exception:  # noqa: BLE001 - 名字解析失败只是「没认出」，不能拖垮整轮
        pass
    return '', ''


def _code_names(codes: List[str]) -> Dict[str, str]:
    """代码 → 规范名（标签展示用；读不到就空，不编名字）。"""
    if not codes:
        return {}
    try:
        from utils.cache.market_data_db import get_market_data_db

        marks = ','.join('?' * len(codes))
        with get_market_data_db() as conn:
            rows = conn.execute(
                'SELECT code, name FROM stock_info WHERE length(code) = 6 '
                'AND code IN (' + marks + ')', list(codes)).fetchall()
        return {str(r[0]): str(r[1]) for r in rows}
    except Exception:  # noqa: BLE001
        return {}


def _clean_token(token: str) -> str:
    text = str(token or '').strip()
    text = re.sub(r'^(?:标的(?:用|是|选|就)?|就用|用|做|跑|看|研究|验证|买|选)+', '', text)
    text = re.sub(r'(?:吧|呢|啊|哦|就行|都可以|这些|那些|龙头|这个|这只|那只|这一个|那一只|的)+$',
                  '', text)
    return text.strip()


def _is_target_like(token: str) -> bool:
    """像不像一个「标的」：用于把没认出来的名字如实告诉用户（不让它静默消失）。"""
    low = token.lower()
    if not token or low in _TARGET_STOP:
        return False
    if any(s in low for s in _TARGET_SKIP_SUBSTR):
        return False
    if 'etf' in low or '基金' in token:
        return True
    return bool(_CHINESE_NAME_RE.fullmatch(token))


def _token_codes(token: str) -> List[str]:
    """单个 token → 代码（别名/俗称/库名）。不判断「要不要报给用户」。

    别名/俗称来自 `market_data.db.symbol_alias`（数据，不是代码）；
    库表缺位时退化为 `stock_info.name` 精确查（见 `_lookup_name`）。
    指数别名不在这里落成代码：它是「范围」，由 `_index_alias_in_text` 处理。
    """
    from backtest.symbol_alias import lookup_group, lookup_one

    low = str(token or '').lower()
    if _index_alias_code(token):
        return []
    one = lookup_one(token)
    if one:
        return [one]
    group = lookup_group(token)
    if group:
        return list(group)
    if low in _TARGET_STOP or not _is_target_like(token):
        return []
    code, name = _lookup_name(token)
    if not code or _index_name_only(code, name or token):
        return []                     # 指数名不是可交易标的（别名库不可用时的兜底）
    return [code]


def _zone_hit_tokens(text: str) -> List[tuple]:
    """每个「点名语境」的 (token 集合, 是否认出过标的)。

    只有「认出过至少一只」的清单句里，没认出的词才值得报给用户；
    「比如我周一买入…胜率高」这种句子一只都没认出，它本来就不是标的清单。
    """
    out = []
    for m in _TARGET_INTRO_RE.finditer(str(text or '')):
        zone_tokens = set()
        for part in _CLAUSE_SPLIT_RE.split(m.group(1)):
            token = _clean_token(part)
            if token:
                zone_tokens.add(token)
        hits = sum(1 for t in zone_tokens if _token_codes(t))
        out.append((zone_tokens, hits))
    return out


def _named_targets(text: str) -> Dict[str, Any]:
    """从原话里抽点名标的：6 位码 / ETF 简称 / 股票名 / 行业俗称。

    返回 {codes, names, unresolved, typed}；`typed` 是用户直接写的 6 位码。
    ``unresolved`` 只报「已经认出过标的」的清单句里的名字（或 ETF/基金类词），
    否则原话里的「我问你」「收获如何」会被当成没认出的标的写进确认卡。
    """
    raw = str(text or '')
    typed = _dedupe_codes(_CODE_RE.findall(raw))
    # 括号里的内容也当候选（「CPO（通信ETF）」→ 通信ETF）
    inner = re.findall(r'[（(]([^）)]{1,40})[）)]', raw)
    body = re.sub(r'[（(][^）)]{0,40}[）)]', ' ', raw)
    tokens: List[str] = []
    for part in _CLAUSE_SPLIT_RE.split(body + ' ' + ' '.join(inner)):
        token = _clean_token(part)
        if token:
            tokens.append(token)
    zones = _zone_hit_tokens(raw)

    codes: List[str] = list(typed)
    names: Dict[str, str] = {}
    unresolved: List[str] = []

    def _add(items: List[str], display: str = '') -> None:
        for item in items:
            if item not in codes:
                codes.append(item)
            if display:
                names.setdefault(item, display)

    for token in tokens:
        if re.fullmatch(r'\d{6}', token):
            continue
        from backtest.symbol_alias import lookup_group, lookup_one
        low = token.lower()
        if _index_alias_code(token):
            continue                      # 指数名是「范围」，不是代码池里的标的
        alias = lookup_one(token)
        if alias:
            _add([alias], _code_names([alias]).get(alias, token))
            continue
        synonym = lookup_group(token)
        if synonym:
            _add(list(synonym))
            continue
        if low in _TARGET_STOP or not _is_target_like(token):
            continue
        code, name = _lookup_name(token)
        if code and not _index_name_only(code, name or token):
            _add([code], name or token)
            continue
        if code:
            # 命到的是指数（别名库不可用时的退化路径）：不是标的，也不当成「没认出」
            continue
        if 'etf' in low or '基金' in token:
            unresolved.append(token)          # ETF 类没认出必须说，用户最容易在这里踩坑
            continue
        if any(hits > 0 and token in zone for zone, hits in zones):
            unresolved.append(token)
    return {'codes': _dedupe_codes(codes), 'names': names,
            'unresolved': unresolved, 'typed': typed}


def parse_scope_details(text: str) -> Dict[str, Any]:
    """原话 → {spec, codes, names, unresolved, source}。

    ⚠️ 点名了具体标的（≥2 个，或**没有**「混合/指数」这类池子词时的单个名字）
    必须落成 `POOL:codes:`，**绝不静默改成默认池/混合池**（真机 2026-10-25 事故）。
    """
    raw = str(text or '').strip()
    out: Dict[str, Any] = {'spec': None, 'codes': [], 'names': {},
                           'unresolved': [], 'source': ''}
    if not raw:
        return out
    named = _named_targets(raw)
    # 认不出的名字在**所有**分支都要带回去（包括「没解析出任何范围」的早退分支）——
    # 少了这一步，用户点名的标的会静默消失（真机 2026-10-25）。
    out['unresolved'] = named['unresolved']
    resolved = named['codes']
    low = raw.lower()
    # 指数范围词（沪深300/中证500…）来自库里的 kind='index' 别名，不是写死的 elif 链
    index_hit = _index_alias_in_text(raw)
    pool_words = ('混合' in raw or index_hit is not None)
    if resolved and (len(resolved) >= 2 or (len(resolved) == 1
                                            and not named['typed'] and not pool_words)):
        source, target = 'codes', ','.join(resolved)
        out.update({'codes': resolved, 'names': named['names'],
                    'unresolved': named['unresolved'], 'source': source})
    else:
        watch = any(k in low for k in ('自选股', '我的自选', '自选里'))
        hold = any(k in low for k in ('我的持仓', '持仓股', '当前持仓', '持仓里'))
        if watch and hold:
            source, target = 'union', ''
        elif watch:
            source, target = 'watchlist', ''
        elif hold:
            source, target = 'portfolio', ''
        elif (('etf' in low and '混合' in raw and ('个股' in raw or '股票' in raw))
              or '混合池' in raw):
            source, target = 'mixed', ''
        elif index_hit:
            source, target = 'index', index_hit[1]
        else:
            m = _BOARD_RE.search(raw)
            name = ''
            if m:
                name = m.group(1)
                if name in _BOARD_STOP:
                    name = ''
            if not name:
                limit_only = _LIMIT_RE.search(raw)
                kind_only = _kind_of(raw)
                if not (limit_only or kind_only in ('stock', 'etf')):
                    return out
                source, target = 'default', ''
            else:
                source, target = 'board', name
        out.update({'unresolved': named['unresolved'], 'source': source})
    limit_m = _LIMIT_RE.search(raw) or _TOTAL_LIMIT_RE.search(raw)
    limit = int(limit_m.group(1)) if limit_m else None
    kind = _kind_of(raw)
    try:
        spec = build_spec(source, target, limit=limit, kind=kind)
    except ValueError:
        return out
    out['spec'] = spec
    return out


def parse_scope_text(text: str) -> Optional[str]:
    """从**用户原话**里抽范围（确定性，不调 LLM）。

    只认封闭说法，宁可留空交给后续「聊」去改，也不猜：
    - 点名 ≥2 个代码 / **中文股名 / 常见 ETF 简称 / 行业俗称** → 代码池
    - 沪深300 / 中证500 / 我的自选 / 我的持仓 / 自选+持仓
    - 「XX板块 / XX行业 / XX概念」
    - 「跑/抽/选/前 N 只」、「只看 ETF / 只看个股」
    """
    return parse_scope_details(text).get('spec')


def _kind_of(text: str) -> str:
    low = str(text or '').lower()
    if any(k in low for k in ('只看etf', '只做etf', '只看 etf', 'etf only')):
        return 'etf'
    if any(k in low for k in ('只看个股', '只做个股', '只做股票', '只看股票')):
        return 'stock'
    return 'all'


def _dedupe_codes(codes: List[str]) -> List[str]:
    out: List[str] = []
    for code in codes:
        if code not in out:
            out.append(code)
    return out


def _watchlist_codes() -> List[str]:
    from utils.cache.core import get_db

    with get_db() as conn:
        rows = conn.execute('SELECT stock_code FROM watchlist ORDER BY added_at').fetchall()
    return _dedupe_codes([str(r[0]) for r in rows
                          if re.fullmatch(r'\d{6}', str(r[0] or ''))])


def _portfolio_codes() -> List[str]:
    try:
        from tools.portfolio_data import get_positions
        positions = (get_positions() or {}).get('positions') or []
    except Exception:  # noqa: BLE001 - 读不到持仓就不是范围，由调用方如实报空
        return []
    return _dedupe_codes([str(p.get('stock_code') or '') for p in positions
                          if re.fullmatch(r'\d{6}', str(p.get('stock_code') or ''))])


def _index_scope(target: str) -> Tuple[List[str], str, str, str]:
    from backtest.symbol_alias import lookup_label

    code = normalize_index_target(target)
    if not code:
        raise ValueError(f'不认识的指数：{target}')
    # 展示名也来自数据（kind='index' 里该代码的第一条中文别名）；查不到就用代码本身
    name = lookup_label(code, kind='index') or code
    from backtest.factor_data import load_universe
    rows = load_universe(code) or []
    if not rows:
        raise ValueError(f'本地没有{name}（{code}）成分股快照，'
                         '先跑 scripts/ops/backfill_index_universe.py 回补')
    return ([str(r['code']) for r in rows], name + ' 成分股',
            str(rows[0].get('as_of_date') or ''), name)


def _board_scope(name: str) -> Tuple[List[str], str, str]:
    from utils.cache.market_data_db import get_market_data_db

    with get_market_data_db() as conn:
        exact = conn.execute(
            'SELECT board_id, board_name, board_type FROM stock_board '
            'WHERE is_active = 1 AND board_name = ?', (name,)).fetchall()
        boards = exact or conn.execute(
            'SELECT board_id, board_name, board_type FROM stock_board '
            'WHERE is_active = 1 AND board_name LIKE ? '
            "ORDER BY CASE WHEN board_type = 'industry' THEN 0 ELSE 1 END, board_name",
            ('%' + name + '%',)).fetchall()
        boards = [dict(b) for b in boards]
        if not boards:
            raise ValueError(f'本地没有叫「{name}」的板块，换个说法或先更新板块数据')
        ids = [b['board_id'] for b in boards]
        marks = ','.join('?' * len(ids))
        rows = conn.execute(
            'SELECT code, MAX(as_of_date) AS as_of FROM stock_board_member '
            f'WHERE board_id IN ({marks}) GROUP BY code', ids).fetchall()
        rows = [dict(r) for r in rows]
    codes = _dedupe_codes([str(r['code']) for r in rows
                           if re.fullmatch(r'\d{6}', str(r['code'] or ''))])
    if not codes:
        raise ValueError(f'板块「{name}」没有本地成分数据')
    names = [str(b['board_name']) for b in boards]
    label = '板块「' + '、'.join(names[:3]) + (f'等 {len(names)} 个' if len(names) > 3 else '') + '」'
    as_of = max((str(r['as_of'] or '') for r in rows), default='')
    return codes, label, as_of


def _mixed_scope() -> Tuple[List[str], str, str]:
    """个股 + ETF 混合：沪深300 成分股 + 内置 ETF 清单（用户要「个股ETF混合」时）。"""
    from backtest.factor_data import load_universe
    from backtest.pool_first import DEFAULT_ETFS

    rows = load_universe('000300') or []
    if not rows:
        raise ValueError('本地没有沪深300成分股快照，先跑 '
                         'scripts/ops/backfill_index_universe.py 回补')
    codes = _dedupe_codes([str(r['code']) for r in rows] + list(DEFAULT_ETFS))
    return codes, '沪深300 + ETF 混合', str(rows[0].get('as_of_date') or '')


def resolve_scope(spec: Any) -> Dict[str, Any]:
    """spec → {codes, label, short, source, as_of, notes}；数据缺失抛 ValueError。"""
    body, opts = _split_options(spec)
    up = body.upper()
    notes: List[str] = []
    if up in ('POOL', 'POOL:DEFAULT', 'DEFAULT', 'DEFAULT_POOL') or body in ('默认', '默认池'):
        from backtest.pool_first import DEFAULT_POOL
        codes, label, short, as_of = list(DEFAULT_POOL), '默认池（25 股票 + 15 ETF）', '默认池', ''
    elif up in ('POOL:SMOKE', 'SMOKE', 'SMOKE_POOL') or body in ('验证池', '烟测池'):
        from backtest.pool_first import SMOKE_POOL
        codes, label, short, as_of = list(SMOKE_POOL), '烟测池（8 股票 + 7 ETF）', '烟测池', ''
    elif up.startswith('POOL:INDEX:'):
        codes, label, as_of, short = _index_scope(body.split(':', 2)[2])
    elif up == 'POOL:WATCHLIST':
        codes = _watchlist_codes()
        if not codes:
            raise ValueError('自选股是空的，先往自选里加几只再跑')
        label, short, as_of = '我的自选', '自选', ''
    elif up == 'POOL:PORTFOLIO':
        codes = _portfolio_codes()
        if not codes:
            raise ValueError('持仓是空的（未同步或确实没有仓位）')
        label, short, as_of = '我的持仓', '持仓', ''
    elif up == 'POOL:UNION':
        codes = _dedupe_codes(_watchlist_codes() + _portfolio_codes())
        if not codes:
            raise ValueError('自选和持仓都是空的')
        label, short, as_of = '自选 ∪ 持仓', '自选+持仓', ''
    elif up == 'POOL:MIXED':
        codes, label, as_of = _mixed_scope()
        short = '混合池'
    elif up.startswith('POOL:BOARD:'):
        board_name = body.split(':', 2)[2].strip()
        codes, label, as_of = _board_scope(board_name)
        short = board_name + '板块'
    elif up.startswith('POOL:CODES:'):
        codes = _dedupe_codes(_CODE_RE.findall(body.split(':', 2)[2]))
        if not codes:
            raise ValueError('没有合法代码')
        # 入口兜底：spec 可能是 LLM/手写的，指数单码在这里也必须被挡（P1）
        _reject_index_codes(codes, 'POOL:codes')
        names = _code_names(codes)
        label, short, as_of = _codes_label(codes, names), '指定 ' + str(len(codes)) + ' 只', ''
    else:
        raise ValueError(f'不认识的范围：{spec}')

    total = len(codes)
    kind = str(opts.get('kind') or 'all').lower()
    if kind in ('stock', 'etf'):
        from backtest.batch_engine import _auto_detect_asset_type
        codes = [c for c in codes if _auto_detect_asset_type(c) == kind]
        notes.append('仅 ' + ('ETF' if kind == 'etf' else '个股'))
        if not codes:
            raise ValueError(('只看 ETF' if kind == 'etf' else '只看个股') + '时这个范围里没有标的')
    limit = opts.get('limit')
    if limit:
        try:
            limit_n = int(limit)
        except ValueError:
            limit_n = 0
        if 0 < limit_n < len(codes):
            codes = _even_sample(codes, limit_n)
            notes.append(f'从 {total} 只里均匀抽 {limit_n} 只')
        total = max(total, len(codes))
    if not codes:
        raise ValueError(f'范围「{label}」里没有可回测的标的')
    label = label + '（' + str(len(codes)) + ' 只'
    if notes:
        label += '，' + '，'.join(notes)
    label += '）'
    if as_of:
        label += '；数据快照 ' + as_of
        notes.append('成分/板块为 ' + as_of + ' 快照，存在幸存者偏差')
    return {'codes': codes, 'label': label, 'short': short,
            'source': body, 'as_of': as_of, 'notes': notes}


def _codes_label(codes: List[str], names: Dict[str, str]) -> str:
    """代码池人话标签：`指定代码：剑桥科技(603083)、通信ETF国泰(515880) 等`。

    必须有名字：只显示「指定代码」时用户看不出自己点的那几只到底进没进池子
    （2026-10-25 用户就是因为这个没发现标的被换掉了）。
    """
    items = []
    for code in codes[:8]:
        name = str(names.get(code) or '').strip()
        items.append(name + '(' + code + ')' if name else code)
    text = '指定代码：' + '、'.join(items)
    if len(codes) > 8:
        text += ' 等'
    return text


def _even_sample(codes: List[str], n: int) -> List[str]:
    """均匀抽样（保持代码排序、可复现）：`codes[int(i*step)]`。"""
    step = len(codes) / float(n)
    return [codes[int(i * step)] for i in range(n)]


def describe_scope(spec: Any) -> str:
    """人话标签；解析失败也**不抛**（确认卡必须先出得来）。"""
    try:
        return str(resolve_scope(spec)['label'])
    except Exception as e:  # noqa: BLE001
        return str(spec) + '（无法解析：' + str(e)[:80] + '）'


def scope_short_label(spec: Any) -> str:
    try:
        return str(resolve_scope(spec)['short'])
    except Exception:  # noqa: BLE001
        return str(spec or '')
