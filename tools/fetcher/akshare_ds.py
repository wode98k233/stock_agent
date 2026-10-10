"""Akshare（东方财富）数据源

防封禁策略：
1. 每次请求前随机休眠 2-5 秒（使用基类 _enforce_rate_limit）
2. 随机轮换 User-Agent（使用基类 _set_random_user_agent）
3. 东财反爬补丁（NID 授权令牌 + 设备指纹）
4. A股历史K线多源降级：东财 → 新浪 → 腾讯
5. 实时行情缓存（使用 data_cache 模块）
6. 单股实时行情优先使用新浪接口（较快）
"""
import logging
import os
import random
import time
from typing import Dict, Any, Optional

import pandas as pd
from .base import DataSource, DataSourceManager, _fetch_tencent_kline, _standardize_stock_code, run_with_timeout
from .cache import get_akshare_cache
from .config import Config
from .data_cache import (
    get_cached_spot, set_cached_spot,
    get_cached_realtime, set_cached_realtime,
    SPOT_CACHE_TTL, REALTIME_CACHE_TTL,
)
from .patches.eastmoney_patch import eastmoney_patch

logger = logging.getLogger("radar.fetcher")

# ETF/港股实时行情缓存（整个 DataFrame，按 symbol 筛选）
_etf_realtime_cache = {'data': None, 'timestamp': 0, 'ttl': 1200}
_hk_realtime_cache = {'data': None, 'timestamp': 0, 'ttl': 1200}


def _run_with_timeout(func, timeout=None):
    """在独立（守护）线程中执行 akshare 调用，超时即返回，不等待挂死线程。

    ⚠️ 不能用 `with ThreadPoolExecutor()`：它退出时 `shutdown(wait=True)` 会 join
    那个可能永远挂住的 akshare 线程，导致超时形同虚设、请求卡死（见 base.run_with_timeout）。
    """
    t = timeout or Config.AKSHARE_CALL_TIMEOUT
    try:
        return run_with_timeout(func, t)
    except TimeoutError:
        raise RuntimeError(f"akshare 调用超时（{t}秒）")


def _match_column(df: pd.DataFrame, candidates: list) -> str | None:
    """在 df 列中按候选表匹配（去空格防御，akshare 列名历史上带空格/连字符漂移）。"""
    normalized = {str(c).replace(" ", "").replace("\u3000", ""): c for c in df.columns}
    for cand in candidates:
        key = cand.replace(" ", "").replace("\u3000", "")
        if key in normalized:
            return normalized[key]
        # 连字符变体：M2 同比 ↔ M2-同比增长
        if key.replace("-", "") in {k.replace("-", "") for k in normalized}:
            return normalized[next(k for k in normalized if k.replace("-", "") == key.replace("-", ""))]
    return None


def _normalize_report_date(raw: str) -> str | None:
    """报告期归一为 YYYY-MM-DD。

    兼容四种源格式（2026-09-06 集成实测）：
    - 完整日期 "2026-08-20"（货币网 LPR）→ 原样
    - 月度 "2026-08"（金十）→ 月末
    - 点分月度 "2026.8"（外管局黄金储备）→ 月末
    - 中文 "2026年07月份" / "2026年7月"（东财 money_supply）→ 月末
    无效返回 None（调用方 dropna）。
    """
    from datetime import date
    import calendar
    import re as _re
    s = str(raw).strip()
    if len(s) >= 10:
        s = s[:10]
        try:
            date.fromisoformat(s)
            return s
        except ValueError:
            return None
    if len(s) == 7 and s[4] == "-":  # YYYY-MM
        try:
            y, m = int(s[:4]), int(s[5:7])
            return f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"
        except ValueError:
            return None
    m = _re.match(r"^(\d{4})\.(\d{1,2})$", s)  # 点分月度（外管局黄金储备）
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            return f"{y:04d}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}"
        return None
    m = _re.match(r"^(\d{4})年(\d{1,2})月", s)  # 中文月份（东财）
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        return f"{y:04d}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}"
    return None


def _build_macro_call_kwargs(api: dict) -> dict:
    """按指标配置构造取数入参。

    部分接口默认返回超长历史（bond_zh_us_rate 无参时从 1990 拉起），首次同步
    会写入上万行且拖慢落库，故用 start_param + lookback_days 声明回看窗口；
    未声明的指标（money_supply/lpr/金十系）无参调用。
    另支持静态 kwargs（如 rate_interbank 的 market/symbol/indicator，接口无日期参数）。

    `end_param` 用于**起止日期都必须显式传**的接口（currency_boc_sina 的默认
    区间写死在源码里，不传就只返回那段固定历史），end 取当天。
    """
    from datetime import date, timedelta
    param = api.get("start_param")
    days = api.get("lookback_days")
    out = {}
    if param and days:
        out[param] = (date.today() - timedelta(days=int(days))).strftime("%Y%m%d")
    end = api.get("end_param")
    if end:
        out[end] = date.today().strftime("%Y%m%d")
    out.update(api.get("kwargs") or {})
    return out


def _calc_dxy() -> pd.DataFrame:
    """国家外汇管理局「人民币汇率中间价」→ 合成美元指数（DXY）。

    为什么走这条路：东财 `index_global_*` 在本机被代理拦截（ProxyError），而外管局
    中间价一次给出 26 种货币，取 6 种按 ICE 官方权重即可合成。

    两个必须注意的点：
    1. 该站证书在本机代理链下过不了标准校验（self signed certificate in certificate
       chain）——这正是台账早期 note 所记「外管局 SSL 拒绝」的真身，故显式 verify=False。
    2. **绝对水平与 ICE 官方 DXY 有偏差**（中间价含政策因子，交叉汇率不等于市场汇率）。
       所以消费方只应使用**变动幅度**、不要用绝对水平设阈值——恒科台账的阈值正是
       「美元趋势 bp」而非水平档，原设计已规避此问题。
    """
    import requests
    from datetime import date, timedelta
    from io import StringIO

    end = date.today()
    # 近 800 天：除当前读数外，还要给恒科 ⑥ 美元趋势因子的「关联性验证」留足样本
    # （60 天只够算读数，不够做散点/分层的回测）
    start = end - timedelta(days=800)
    with requests.Session() as s:
        s.verify = False
        r = s.post("https://www.safe.gov.cn/AppStructured/hlw/RMBQuery.do",
                   data={"startDate": start.isoformat(), "endDate": end.isoformat(),
                         "queryYN": "true"}, timeout=30)
    df = pd.read_html(StringIO(r.text))[-1]

    def px(col: str):
        # 中间价按「分」报价（6.7580 报 675.80）
        return pd.to_numeric(df[col], errors="coerce") / 100.0

    usd = px("美元")                       # 日元口径本就是「每 100 日元」
    eurusd = px("欧元") / usd
    usdjpy = usd / px("日元")
    gbpusd = px("英镑") / usd
    usdcad = usd / px("加元")
    usdsek = usd / px("瑞典克朗")
    usdchf = usd / px("瑞士法郎")
    dxy = (50.14348112 * eurusd ** -0.576 * usdjpy ** 0.136 * gbpusd ** -0.119
           * usdcad ** 0.091 * usdsek ** 0.042 * usdchf ** 0.036)

    out = pd.DataFrame({
        "report_date": pd.to_datetime(df["日期"], errors="coerce").dt.strftime("%Y-%m-%d"),
        "value": dxy.astype(float).round(4),
    }).dropna(subset=["report_date", "value"])
    return out.sort_values("report_date").reset_index(drop=True)


_SINA_FUTURE_SYMBOLS = {
    "CU0": "沪铜主连",
    "RB0": "螺纹钢主连",
}


def _calc_sina_future_main(symbol: str) -> pd.DataFrame:
    """新浪期货主连收盘价 → [report_date, value]（铜周期台账专用）。

    为什么单独包一层而不是交给 `_MACRO_APIS` 的通用列名匹配：`futures_main_sina`
    返回中文列（日期/收盘价），必须同时指定 date 与 value 两列并派生闭包，
    通用表只支持固定 kwargs，无法把「取哪一列」写进去。这里用 calc 表规避。

    **主连序列的本质**：它是主力合约的拼接序列，跨月换月处存在跳价。看趋势、
    看历史分位可靠；看某一天的绝对价位需要注意换月点附近可能有台阶。
    """
    ak = AkshareDataSource._get_ak()

    def _fetch():
        from datetime import date as _d
        return ak.futures_main_sina(symbol=symbol, start_date="20050101",
                                    end_date=_d.today().strftime("%Y%m%d"))

    df = _run_with_timeout(_fetch, 60)
    if df is None or df.empty:
        raise RuntimeError(f"akshare 未获取到期货主连数据({symbol})")
    close_col = _match_column(df, ["收盘价", "close", "收盘"])
    date_col = _match_column(df, ["日期", "date"])
    if close_col is None or date_col is None:
        raise RuntimeError(f"期货主连 {symbol} 列名漂移: 实际列 {list(df.columns)}")
    out = pd.DataFrame({
        "report_date": pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d"),
        "value": pd.to_numeric(df[close_col], errors="coerce"),
    }).dropna(subset=["report_date", "value"])
    return out.sort_values("report_date").reset_index(drop=True)


# 本地计算型指标的实现表（`_MACRO_APIS` 用 "calc" 引用）。
# 与直取型的区别：源站给的是多列原始材料，需要本地换算成单一 report_date/value 序列。
_MACRO_CALCS = {
    "_calc_dxy": _calc_dxy,
    "_calc_cu_main": lambda: _calc_sina_future_main("CU0"),
    "_calc_rb_main": lambda: _calc_sina_future_main("RB0"),
}


def _is_csindex_code(code: str) -> bool:
    """中证指数公司代码判定。两种形态：

    - `H` + 纯数字：H30269 红利低波、H30374 等（中证「H 系列」）
    - 6 位纯数字且以 `93/94` 开头：930914 中证港股通高股息投资、931xxx 等
      （中证自编段；这批在交易所无对应代码，只能走中证自编接口）

    必须与港股指数区分——港股指数是 H + 字母（HSTECH / HSI / HSHDYI）。
    历史上 `code.startswith("H")` 的粗判会把 H30269 误判成港股指数。

    ⚠️ **不可**把 `000xxx` 纳入：000300（沪深300）是与交易所代码重叠的真·交易所
    指数（日线走 `stock_zh_index_daily_em('sh000300')`），混进来会取错源。
    """
    s = str(code).strip().upper()
    if len(s) >= 5 and s[0] == "H" and s[1:].isdigit():
        return True
    return len(s) == 6 and s.isdigit() and s[:2] in ("93", "94")


def macro_freshness_days(df: pd.DataFrame) -> int | None:
    """宏观指标新鲜度：最新数据期距今天数（空 DataFrame 返回 None）。

    >400 天 = 源数据滞后（金十系实测滞后约 1 年），前端需标注。
    """
    if df is None or df.empty or "report_date" not in df.columns:
        return None
    from datetime import date, datetime
    latest = str(df["report_date"].max())
    try:
        latest_date = datetime.strptime(latest[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return (date.today() - latest_date).days


class AkshareDataSource(DataSource):
    name: str = "akshare"
    label: str = "Akshare"
    description: str = "开源聚合接口，支持 A/HK/ETF"
    priority: int = int(os.getenv("AKSHARE_PRIORITY", "90"))
    _ak = None
    _patch_applied = False

    @classmethod
    def _get_ak(cls):
        if cls._ak is None:
            try:
                import akshare as ak
                cls._ak = ak
            except ImportError:
                logger.error("akshare 未安装，禁用")
                cls.enabled = False
                raise
        return cls._ak

    @classmethod
    def _ensure_patch(cls):
        """确保东财反爬补丁已启用"""
        if not cls._patch_applied and Config.AKSHARE_ENABLE_EASTMONEY_PATCH:
            try:
                eastmoney_patch()
                cls._patch_applied = True
            except Exception as e:
                logger.warning(f"东财反爬补丁启用失败: {e}")

    @classmethod
    def is_available(cls) -> bool:
        try:
            cls._get_ak()
            cls._ensure_patch()  # 启用补丁
            return cls.enabled
        except:
            return False

    @classmethod
    def get_stock_hist(cls, symbol, period="daily", start="", end=""):
        """获取历史K线数据，支持多源降级：东财 → 新浪 → 腾讯"""
        ak = cls._get_ak()
        market = DataSourceManager.detect_market(symbol)

        if market == "hk":
            return cls._fetch_hk_hist(ak, symbol, period, start, end)
        elif market == "etf":
            return cls._fetch_etf_hist(ak, symbol, period, start, end)
        else:
            return cls._fetch_cn_hist(ak, symbol, period, start, end)

    @classmethod
    def _fetch_cn_hist(cls, ak, symbol, period, start, end):
        """A股历史K线：东财 → 新浪 → 腾讯 三源降级"""
        # 1. 东财接口
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            logger.info(f"[akshare] 尝试东财接口获取 {symbol}...")
            df = _run_with_timeout(
                lambda: ak.stock_zh_a_hist(
                    symbol=symbol, period=period,
                    start_date=start.replace('-', ''), end_date=end.replace('-', ''),
                    adjust="qfq"
                )
            )
            if df is not None and not df.empty:
                logger.info(f"[akshare] 东财接口获取成功: {len(df)} 行")
                return df
        except Exception as e:
            logger.warning(f"[akshare] 东财接口失败: {e}")

        # 2. 新浪接口
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            sina_symbol = _standardize_stock_code(symbol)  # sh600000, sz000001
            logger.info(f"[akshare] 尝试新浪接口获取 {symbol} (symbol={sina_symbol})...")
            df = _run_with_timeout(
                lambda: ak.stock_zh_a_daily(
                    symbol=sina_symbol,
                    start_date=start.replace('-', ''), end_date=end.replace('-', ''),
                    adjust="qfq"
                )
            )
            if df is not None and not df.empty:
                # 标准化列名
                rename_map = {
                    'date': '日期', 'open': '开盘', 'high': '最高',
                    'low': '最低', 'close': '收盘', 'volume': '成交量', 'amount': '成交额'
                }
                df = df.rename(columns=rename_map)
                if '收盘' in df.columns and '涨跌幅' not in df.columns:
                    df['涨跌幅'] = df['收盘'].pct_change() * 100
                    df['涨跌幅'] = df['涨跌幅'].fillna(0)
                logger.info(f"[akshare] 新浪接口获取成功: {len(df)} 行")
                return df
        except Exception as e:
            logger.warning(f"[akshare] 新浪接口失败: {e}")

        # 3. 腾讯接口（使用 base 的通用函数）
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            tx_code = _standardize_stock_code(symbol)
            logger.info(f"[akshare] 尝试腾讯接口获取 {symbol} (code={tx_code})...")
            headers = cls._set_random_user_agent()
            df = _fetch_tencent_kline(tx_code, period, start, end, headers)
            if df is not None and not df.empty:
                logger.info(f"[akshare] 腾讯接口获取成功: {len(df)} 行")
                return df
        except Exception as e:
            logger.warning(f"[akshare] 腾讯接口失败: {e}")

        raise RuntimeError(f"akshare 所有渠道获取 {symbol} 失败")

    @classmethod
    def _fetch_hk_hist(cls, ak, symbol, period, start, end):
        """港股历史K线"""
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            code = symbol.lower().replace('hk', '').replace('.hk', '').zfill(5)
            df = _run_with_timeout(
                lambda: ak.stock_hk_hist(
                    symbol=code, period=period,
                    start_date=start.replace('-', ''), end_date=end.replace('-', ''),
                    adjust="qfq"
                )
            )
            if df is not None and not df.empty:
                return df
        except Exception as e:
            logger.warning(f"[akshare] 港股历史K线失败: {e}")
        raise RuntimeError(f"akshare 获取港股 {symbol} 历史K线失败")

    @classmethod
    def _fetch_etf_hist(cls, ak, symbol, period, start, end):
        """ETF历史K线"""
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = _run_with_timeout(
                lambda: ak.fund_etf_hist_em(
                    symbol=symbol, period=period,
                    start_date=start.replace('-', ''), end_date=end.replace('-', ''),
                    adjust="qfq"
                )
            )
            if df is not None and not df.empty:
                # 标准化 ETF 列名
                rename = {}
                for col in df.columns:
                    if col in ("开盘价",):
                        rename[col] = "开盘"
                    elif col in ("收盘价",):
                        rename[col] = "收盘"
                    elif col in ("最高价",):
                        rename[col] = "最高"
                    elif col in ("最低价",):
                        rename[col] = "最低"
                if rename:
                    df = df.rename(columns=rename)
                return df
        except Exception as e:
            logger.warning(f"[akshare] ETF历史K线失败: {e}")
        raise RuntimeError(f"akshare 获取ETF {symbol} 历史K线失败")

    @classmethod
    def get_spot_em(cls):
        """
        获取全市场行情（带缓存 + 降级）

        优先级：
        1. 缓存
        2. 东财接口 (stock_zh_a_spot_em)
        3. 新浪接口 (stock_zh_a_spot) - 降级，较慢
        """
        # 检查缓存
        cached_data = get_cached_spot()
        if cached_data is not None:
            logger.debug("[akshare] 使用缓存的全市场行情")
            return cached_data

        ak = cls._get_ak()

        # 尝试东财接口
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            logger.info("[akshare] 尝试东财接口获取全市场行情...")
            df = _run_with_timeout(ak.stock_zh_a_spot_em)
            if df is not None and not df.empty:
                set_cached_spot(df, SPOT_CACHE_TTL)
                logger.info(f"[akshare] 东财接口成功，缓存已更新，TTL={SPOT_CACHE_TTL}s")
                return df
        except Exception as e:
            logger.warning(f"[akshare] 东财接口失败: {e}，尝试新浪接口")

        # 降级到新浪接口
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            logger.info("[akshare] 尝试新浪接口获取全市场行情（较慢）...")
            df = _run_with_timeout(ak.stock_zh_a_spot, timeout=60)  # 新浪接口较慢，增加超时
            if df is not None and not df.empty:
                set_cached_spot(df, SPOT_CACHE_TTL)
                logger.info(f"[akshare] 新浪接口成功，缓存已更新，TTL={SPOT_CACHE_TTL}s")
                return df
        except Exception as e:
            logger.warning(f"[akshare] 新浪接口也失败: {e}")

        raise RuntimeError("akshare 获取全市场行情失败")

    @classmethod
    def get_stock_realtime(cls, symbol: str) -> pd.DataFrame:
        """
        获取单只股票实时行情

        优先级：
        1. 缓存
        2. 全市场行情（如果已缓存）
        3. 新浪单股接口（较快）
        """
        # 检查缓存
        from .data_cache import get_cached_realtime
        cached = get_cached_realtime(symbol)
        if cached is not None:
            logger.debug(f"[akshare] 使用缓存的 {symbol} 实时行情")
            return cached

        market = DataSourceManager.detect_market(symbol)

        if market == "hk":
            return cls._get_hk_realtime(cls._get_ak(), symbol)
        elif market == "etf":
            return cls._get_etf_realtime(cls._get_ak(), symbol)

        # A 股：先尝试全市场缓存
        spot_cache = get_cached_spot()
        if spot_cache is not None and not spot_cache.empty:
            for code_col in ('代码', '股票代码', 'code', 'symbol'):
                if code_col in spot_cache.columns:
                    result = spot_cache[spot_cache[code_col].astype(str) == str(symbol)]
                    if not result.empty:
                        logger.debug(f"[akshare] 从全市场缓存获取 {symbol}")
                        return result.copy()

        # 全市场缓存没有，尝试新浪单股接口（较快）
        try:
            cls._enforce_rate_limit(min_seconds=1.0, max_seconds=2.0)
            logger.info(f"[akshare] 尝试新浪接口获取 {symbol} 实时行情...")
            df = cls._get_sina_realtime(symbol)
            if df is not None and not df.empty:
                return df
        except Exception as e:
            logger.warning(f"[akshare] 新浪接口获取 {symbol} 失败: {e}")

        # 最后尝试全市场接口
        try:
            df = cls.get_spot_em()
            if df is not None and not df.empty:
                for code_col in ('代码', '股票代码', 'code', 'symbol'):
                    if code_col in df.columns:
                        result = df[df[code_col].astype(str) == str(symbol)]
                        if not result.empty:
                            return result.copy()
        except Exception as e:
            logger.warning(f"[akshare] 全市场接口获取 {symbol} 失败: {e}")

        return pd.DataFrame()

    @classmethod
    def _get_sina_realtime(cls, symbol: str) -> pd.DataFrame:
        """通过新浪接口获取单股实时行情（较快）"""
        import requests

        # 转换代码格式
        if symbol.startswith('6'):
            code = f"sh{symbol}"
        else:
            code = f"sz{symbol}"

        url = f"https://hq.sinajs.cn/list={code}"
        headers = {
            'Referer': 'https://finance.sina.com.cn',
            'User-Agent': random.choice(cls._USER_AGENTS),
        }

        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.encoding = 'gbk'

            if response.status_code != 200:
                return pd.DataFrame()

            content = response.text.strip()
            if '=""' in content:
                return pd.DataFrame()

            # 解析数据
            data_start = content.find('"')
            data_end = content.rfind('"')
            if data_start == -1 or data_end == -1:
                return pd.DataFrame()

            data_str = content[data_start+1:data_end]
            fields = data_str.split(',')

            if len(fields) < 32:
                return pd.DataFrame()

            # 计算涨跌幅
            price = float(fields[3]) if fields[3] else 0
            pre_close = float(fields[2]) if fields[2] else 0
            change_pct = 0
            if pre_close > 0:
                change_pct = (price - pre_close) / pre_close * 100

            df = pd.DataFrame([{
                '代码': symbol,
                '名称': fields[0],
                '最新价': price,
                '今开': float(fields[1]) if fields[1] else 0,
                '最高': float(fields[4]) if fields[4] else 0,
                '最低': float(fields[5]) if fields[5] else 0,
                '昨收': pre_close,
                '成交量': int(float(fields[8])) if fields[8] else 0,
                '成交额': float(fields[9]) if fields[9] else 0,
                '涨跌幅': change_pct,
                '涨跌额': price - pre_close,
            }])

            # 缓存结果
            from .data_cache import set_cached_realtime
            set_cached_realtime(symbol, df, REALTIME_CACHE_TTL)

            return df

        except Exception as e:
            logger.debug(f"[akshare] 新浪接口解析失败: {e}")
            return pd.DataFrame()

    @classmethod
    def _get_etf_realtime(cls, ak, symbol: str) -> pd.DataFrame:
        """获取ETF实时行情（带缓存）"""
        current_time = time.time()

        if (_etf_realtime_cache['data'] is not None and
            current_time - _etf_realtime_cache['timestamp'] < _etf_realtime_cache['ttl']):
            df = _etf_realtime_cache['data']
        else:
            try:
                cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
                df = ak.fund_etf_spot_em()
                _etf_realtime_cache['data'] = df
                _etf_realtime_cache['timestamp'] = current_time
            except Exception as e:
                logger.debug(f"akshare ETF 实时失败: {e}")
                return pd.DataFrame()

        if df is not None and not df.empty:
            for code_col in ("代码", "基金代码", "code"):
                if code_col in df.columns:
                    return df[df[code_col].astype(str) == str(symbol)].copy()
        return pd.DataFrame()

    @classmethod
    def _get_hk_realtime(cls, ak, symbol: str) -> pd.DataFrame:
        """获取港股实时行情（带缓存）"""
        current_time = time.time()

        if (_hk_realtime_cache['data'] is not None and
            current_time - _hk_realtime_cache['timestamp'] < _hk_realtime_cache['ttl']):
            df = _hk_realtime_cache['data']
        else:
            try:
                cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
                df = ak.stock_hk_spot_em()
                _hk_realtime_cache['data'] = df
                _hk_realtime_cache['timestamp'] = current_time
            except Exception as e:
                logger.debug(f"akshare 港股实时失败: {e}")
                return pd.DataFrame()

        if df is not None and not df.empty:
            for code_col in ("代码", "股票代码", "code"):
                if code_col in df.columns:
                    return df[df[code_col].astype(str) == str(symbol)].copy()
        return pd.DataFrame()

    @classmethod
    def get_board_industry_cons(cls, symbol):
        cls._enforce_rate_limit(min_seconds=1.0, max_seconds=3.0)
        return cls._get_ak().stock_board_industry_cons_em(symbol=symbol)

    @classmethod
    def get_board_concept_cons(cls, symbol):
        cls._enforce_rate_limit(min_seconds=1.0, max_seconds=3.0)
        return cls._get_ak().stock_board_concept_cons_em(symbol=symbol)

    @classmethod
    def get_board_industry_list(cls):
        cls._enforce_rate_limit(min_seconds=1.0, max_seconds=3.0)
        return cls._get_ak().stock_board_industry_name_em()

    @classmethod
    def get_board_concept_list(cls):
        cls._enforce_rate_limit(min_seconds=1.0, max_seconds=3.0)
        return cls._get_ak().stock_board_concept_name_em()

    @classmethod
    def get_stock_news(cls, symbol):
        cls._enforce_rate_limit(min_seconds=1.0, max_seconds=3.0)
        return cls._get_ak().stock_news_em(symbol=symbol)

    @classmethod
    def get_market_news(cls, query: str = "今日A股市场热点新闻", limit: int = 50):
        cls._enforce_rate_limit(min_seconds=1.0, max_seconds=3.0)
        ak = cls._get_ak()
        df = ak.stock_info_global_em()
        # 统一列名
        df = df.rename(columns={
            "标题": "title",
            "摘要": "summary",
            "发布时间": "publish_time",
            "链接": "url",
        })
        df["source"] = "东方财富"
        # 按时间倒序
        if "publish_time" in df.columns:
            df = df.sort_values("publish_time", ascending=False)
        if limit and len(df) > limit:
            df = df.head(limit)
        return df

    @classmethod
    def get_stock_rating(cls, symbol):
        ak = cls._get_ak()
        cache = get_akshare_cache()

        # 先试试原来的方法（快速）
        for method in [
            lambda: ak.stock_institute_recommend_detail(symbol=symbol),
            lambda: ak.stock_institute_recommend(symbol="股票综合评级").query(f'股票代码 == "{symbol}"'),
            lambda: ak.stock_institute_recommend(symbol="投资评级选股").query(f'股票代码 == "{symbol}"'),
        ]:
            try:
                cls._enforce_rate_limit(min_seconds=1.0, max_seconds=2.0)
                df = method()
                if not df.empty:
                    return df
            except:
                continue

        # 如果原来的方法不行，再试试千股千评（慢，但有缓存）
        try:
            df_all = cache.get_comment_data()
            if df_all is None:
                logger.info("获取千股千评全量数据（可能需要30-60秒）...")
                cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
                df_all = _run_with_timeout(ak.stock_comment_em, timeout=60)
                if not df_all.empty:
                    cache.set_comment_data(df_all)

            if not df_all.empty and '代码' in df_all.columns:
                df = df_all[df_all['代码'] == symbol].copy()
                if not df.empty:
                    return df
        except Exception as e:
            logger.debug(f"akshare stock_comment_em 失败: {e}")

        return pd.DataFrame()

    @classmethod
    def get_financial_abstract(cls, symbol):
        cls._enforce_rate_limit(min_seconds=1.0, max_seconds=3.0)
        return cls._get_ak().stock_financial_abstract_ths(symbol=symbol)

    @classmethod
    def get_valuation_indicators(cls, symbol: str) -> pd.DataFrame:
        """获取个股估值指标（PE/PB/PS/PEG/股息率/EV_EBITDA）"""
        ak = cls._get_ak()
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = _run_with_timeout(lambda: ak.stock_a_indicator_lg(symbol=symbol))
            if df is not None and not df.empty:
                return df.tail(1)
        except Exception as e:
            logger.debug(f"akshare stock_a_indicator_lg 失败: {e}")

        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            spot = _run_with_timeout(ak.stock_zh_a_spot_em)
            row = spot[spot['代码'] == symbol]
            if not row.empty:
                r = row.iloc[0]
                return pd.DataFrame([{
                    '股票代码': symbol,
                    '市盈率-动态': float(r.get('市盈率-动态', 0) or 0),
                    '市净率': float(r.get('市净率', 0) or 0),
                    '总市值': float(r.get('总市值', 0) or 0),
                }])
        except Exception as e:
            logger.debug(f"akshare spot_em 降级获取估值失败: {e}")

        raise RuntimeError(f"akshare 未获取到 {symbol} 的估值数据")

    @classmethod
    def get_valuation_history(cls, symbol: str, years: int = 5) -> pd.DataFrame:
        """个股历史估值序列（列 `trade_date` / `pe_ttm` / `pb`，按日期升序）。

        数据源（2026-09-21 修）：原实现调 `ak.stock_a_indicator_lg`（乐咕乐股），
        **该接口在 akshare 1.18.x 已被移除**，`hasattr` 为空 → 每次都在
        `except Exception` 里被吞成 `RuntimeError("akshare 未获取到历史估值数据")`。
        这就是报告里 `pe_percentile` 长期只能是「无分位」的直接原因：
        `agents/analysis/extractors.py::extract_pe_percentile` 能消费分位，
        但上游根本没有序列可算。

        改用百度股市通 `stock_zh_valuation_baidu`：分别取 PE(TTM) 与市净率两条
        日频序列，按日期外连接合并（实测 603369 近一年 366 行，最新值 14.51/2.08
        与行情快照一致）。若环境里仍存在旧接口，则优先走旧接口（列更全、省一次请求）。
        输出列名与 `tools/valuation.py::calc_percentile` 的契约一致。
        """
        ak = cls._get_ak()
        market = DataSourceManager.detect_market(symbol)
        if market in ("hk", "us", "etf"):
            raise RuntimeError(f"akshare 估值历史仅支持 A 股（当前 {market}）")

        if hasattr(ak, "stock_a_indicator_lg"):
            try:
                cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN,
                                        max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
                legacy = ak.stock_a_indicator_lg(symbol=symbol)
                if legacy is not None and not legacy.empty:
                    if "trade_date" in legacy.columns:
                        legacy["trade_date"] = pd.to_datetime(legacy["trade_date"])
                        cutoff = pd.Timestamp.now() - pd.DateOffset(years=years)
                        legacy = legacy[legacy["trade_date"] >= cutoff]
                    if not legacy.empty:
                        return legacy
            except Exception as e:
                logger.debug(f"akshare stock_a_indicator_lg 不可用，回落百度估值: {e}")

        if not hasattr(ak, "stock_zh_valuation_baidu"):
            raise RuntimeError("akshare 当前版本无可用个股估值历史接口"
                               "（stock_a_indicator_lg / stock_zh_valuation_baidu 都缺失）")

        period = ("近一年" if years <= 1 else "近三年" if years <= 3 else
                  "近五年" if years <= 5 else "近十年" if years <= 10 else "全部")

        def _series(indicator: str, col: str, skip_absurd: bool) -> Optional[pd.DataFrame]:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN,
                                    max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_zh_valuation_baidu(symbol=symbol, indicator=indicator, period=period)
            if df is None or df.empty:
                return None
            df = df.rename(columns={df.columns[0]: "trade_date", df.columns[-1]: col})
            df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
            df[col] = pd.to_numeric(df[col], errors="coerce")
            if skip_absurd:
                # 亏损股的 PE 会是负值/异常大，分位计算只认正值（由 calc_percentile 再兜一层）
                df.loc[df[col] <= 0, col] = None
            return df[["trade_date", col]].dropna(subset=["trade_date"])

        try:
            pe_df = _series("市盈率(TTM)", "pe_ttm", skip_absurd=False)
            pb_df = _series("市净率", "pb", skip_absurd=False)
        except Exception as e:
            raise RuntimeError(f"akshare 获取估值历史失败: {e}")

        frames = [f for f in (pe_df, pb_df) if f is not None and not f.empty]
        if not frames:
            raise RuntimeError(f"akshare 未获取到 {symbol} 的历史估值数据")

        merged = frames[0]
        for extra in frames[1:]:
            merged = merged.merge(extra, on="trade_date", how="outer")
        cutoff = pd.Timestamp.now() - pd.DateOffset(years=years)
        merged = merged[merged["trade_date"] >= cutoff].sort_values("trade_date")
        if merged.empty:
            raise RuntimeError(f"akshare 未获取到 {symbol} 近 {years} 年的估值数据")
        return merged.reset_index(drop=True)

    @classmethod
    def get_industry_valuation(cls, industry_name: str) -> pd.DataFrame:
        """获取行业估值统计（基于板块成分股聚合）"""
        ak = cls._get_ak()
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df_cons = ak.stock_board_industry_cons_em(symbol=industry_name)
            if df_cons is None or df_cons.empty:
                return pd.DataFrame()

            code_col = '代码' if '代码' in df_cons.columns else None
            if code_col is None:
                return pd.DataFrame()

            codes = df_cons[code_col].tolist()[:50]

            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            spot = ak.stock_zh_a_spot_em()
            industry_stocks = spot[spot['代码'].isin(codes)]

            if industry_stocks.empty:
                return pd.DataFrame()

            pe_col = '市盈率-动态' if '市盈率-动态' in industry_stocks.columns else None
            pb_col = '市净率' if '市净率' in industry_stocks.columns else None

            result = {'行业': industry_name, '股票数量': len(industry_stocks)}
            if pe_col:
                valid_pe = industry_stocks[pe_col].replace([float('inf'), float('-inf')], pd.NA).dropna()
                valid_pe = valid_pe[valid_pe > 0]
                result['PE均值'] = round(float(valid_pe.mean()), 2) if len(valid_pe) > 0 else 0
                result['PE中位数'] = round(float(valid_pe.median()), 2) if len(valid_pe) > 0 else 0
            if pb_col:
                valid_pb = industry_stocks[pb_col].replace([float('inf'), float('-inf')], pd.NA).dropna()
                valid_pb = valid_pb[valid_pb > 0]
                result['PB均值'] = round(float(valid_pb.mean()), 2) if len(valid_pb) > 0 else 0
                result['PB中位数'] = round(float(valid_pb.median()), 2) if len(valid_pb) > 0 else 0

            return pd.DataFrame([result])
        except Exception as e:
            logger.debug(f"akshare 行业估值获取失败: {e}")
        raise RuntimeError(f"akshare 未获取到行业 {industry_name} 的估值数据")

    # ── 资金流向 ──

    @classmethod
    def get_individual_fund_flow(cls, symbol: str) -> pd.DataFrame:
        """获取个股资金流向（主力/散户/净流入）"""
        ak = cls._get_ak()
        market = DataSourceManager.detect_market(symbol)

        if market == "etf":
            try:
                cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
                df = ak.fund_etf_fund_flow_em(symbol=symbol)
                if df is not None and not df.empty:
                    return df
                raise RuntimeError(f"akshare 未获取到 ETF {symbol} 的资金流向数据")
            except RuntimeError:
                raise
            except Exception as e:
                raise RuntimeError(f"akshare 获取 ETF 资金流向失败: {e}")

        if market == "hk":
            raise RuntimeError(f"akshare 暂不支持港股 {symbol} 的资金流向")

        if market == "us":
            raise RuntimeError(f"akshare 暂不支持美股 {symbol} 的资金流向")

        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            mkt = "sh" if symbol.startswith("6") else "sz"
            df = ak.stock_individual_fund_flow(symbol=symbol, market=mkt)
            if df is None or df.empty:
                raise RuntimeError(f"akshare 未获取到 {symbol} 的资金流向数据")
            return df
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"akshare 获取资金流向失败: {e}")

    @classmethod
    def get_sector_fund_flow_rank(cls, indicator: str = "今日", sector_type: str = "行业资金流") -> pd.DataFrame:
        """获取板块资金流向排名"""
        ak = cls._get_ak()
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_sector_fund_flow_rank(indicator=indicator, sector_type=sector_type)
            if df is None or df.empty:
                raise RuntimeError(f"akshare 未获取到板块资金流向排名数据({indicator}/{sector_type})")
            return df
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"akshare 获取板块资金流向排名失败: {e}")

    @classmethod
    def get_north_fund_flow(cls, symbol: str = "北向资金") -> pd.DataFrame:
        """获取北向资金历史数据"""
        ak = cls._get_ak()
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_hsgt_hist_em(symbol=symbol)
            if df is None or df.empty:
                raise RuntimeError(f"akshare 未获取到北向资金数据({symbol})")
            return df
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"akshare 获取北向资金失败: {e}")

    # ── 融资融券 ──

    @classmethod
    def get_margin_trading(cls, market: str = "sh", start_date: str = "", end_date: str = "") -> pd.DataFrame:
        """获取融资融券汇总数据"""
        ak = cls._get_ak()
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            if market == "sh":
                df = ak.stock_margin_sse(start_date=start_date, end_date=end_date)
            else:
                df = ak.stock_margin_szse(start_date=start_date, end_date=end_date)
            if df is None or df.empty:
                raise RuntimeError(f"akshare 未获取到融资融券汇总数据({market})")
            return df
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"akshare 获取融资融券汇总失败: {e}")

    # ── 宏观数据 ──
    # 逐指标选源（2026-09-05 实测：金十源 GDP/CPI/PMI 滞后约 1 年，东财/货币网新鲜）。
    # 列名为中文且随 akshare 版本漂移，每指标配候选列名表做防御性 rename；
    # raw 原始行由 utils/cache/macro_db.py 落 raw_json，漂移时可回溯重解析。

    _MACRO_APIS = {
        # 东财 money_supply（新鲜，覆盖 2008 至今；2026-09-06 集成实测锁定列名）
        "m2_yoy":  {"fn": "macro_china_money_supply", "date": ["月份"], "value": ["货币和准货币(M2)-同比增长", "M2 同比", "M2-同比增长"], "period": "monthly"},
        "m1_yoy":  {"fn": "macro_china_money_supply", "date": ["月份"], "value": ["货币(M1)-同比增长", "M1 同比", "M1-同比增长"], "period": "monthly"},
        "m0_yoy":  {"fn": "macro_china_money_supply", "date": ["月份"], "value": ["流通中的现金(M0)-同比增长", "M0 同比", "M0-同比增长"], "period": "monthly"},
        # 货币网 LPR（新鲜，月度 20 日）
        "lpr_1y":  {"fn": "macro_china_lpr", "date": ["TRADE_DATE"], "value": ["LPR1Y"], "period": "daily"},
        "lpr_5y":  {"fn": "macro_china_lpr", "date": ["TRADE_DATE"], "value": ["LPR5Y"], "period": "daily"},
        # 金十系（滞后约 1 年，历史区间分析仍有价值；前端标注最后数据期）
        "cpi_yoy": {"fn": "macro_china_cpi_yearly", "date": ["日期"], "value": ["今值"], "period": "monthly"},
        "gdp_yoy": {"fn": "macro_china_gdp_yearly", "date": ["日期"], "value": ["今值"], "period": "quarterly"},
        "pmi_mfg": {"fn": "macro_china_pmi_yearly", "date": ["日期"], "value": ["今值"], "period": "monthly"},
        # 中债/美债收益率曲线（东财 bond_zh_us_rate，日频，实测新鲜到当日）。
        # 该接口无参调用会从 1990 年拉起全量，必须用 lookback_days 限定窗口。
        # 注意：同一接口一次返回中美多条曲线，逐指标各拉一次（可接受，日频数据量小）。
        "cn10_y": {"fn": "bond_zh_us_rate", "date": ["日期"], "value": ["中国国债收益率10年"],
                   "period": "daily", "start_param": "start_date", "lookback_days": 400},
        "cn30_y": {"fn": "bond_zh_us_rate", "date": ["日期"], "value": ["中国国债收益率30年"],
                   "period": "daily", "start_param": "start_date", "lookback_days": 400},
        "us10_y": {"fn": "bond_zh_us_rate", "date": ["日期"], "value": ["美国国债收益率10年"],
                   "period": "daily", "start_param": "start_date", "lookback_days": 400},
        # 银行间资金面（债基台账的流动性锚）。该源只覆盖同业拆借市场，**不含 DR007/R007**
        # （质押式回购），故用 Shibor 1W 作资金面宽松度代理；接口无日期参数，返回全量历史。
        "shibor_1w": {"fn": "rate_interbank", "date": ["报告日", "日期"],
                      "value": ["利率", "利率(%)"], "period": "daily",
                      "kwargs": {"market": "上海银行同业拆借市场",
                                 "symbol": "Shibor人民币", "indicator": "1周"}},
        # 人民币汇率（中行牌价，新浪源；日频，当日即有值）。取「央行中间价」而非
        # 中行汇买价：中间价是政策基准口径，与台账里「货币政策宽松空间」的约束语义一致。
        # 该接口默认区间写死在源码里（2023-03~2023-11），必须同时给起止日期。
        # 单位是「分」（6.7459 报 674.59），故声明 value_scale=0.01。
        "usdcny": {"fn": "currency_boc_sina", "date": ["日期"],
                   "value": ["央行中间价"], "period": "daily",
                   "start_param": "start_date", "lookback_days": 400,
                   "end_param": "end_date", "kwargs": {"symbol": "美元"},
                   "value_scale": 0.01},
        # 港元 HIBOR 一周定价（香港银行公会，日频）。恒生科技台账分母端的流动性锚：
        # 联系汇率下港元利率直接反映港元流动性松紧。该接口无日期参数，
        # 一次返回 2017 年至今全量（约 2300 行），由增量落库过滤。
        "hk_hibor_1w": {"fn": "macro_china_hk_market_info", "date": ["日期"],
                        "value": ["1W-定价"], "period": "daily"},
        # 合成美元指数（外管局人民币汇率中间价 → ICE 官方 DXY 公式，见 _calc_dxy）。
        # **只用于趋势判断**：绝对水平与官方 ICE DXY 有偏差（中间价含政策因子），
        # 故台账阈值设成「趋势 bp」而不是水平档。
        "dxy": {"calc": "_calc_dxy", "period": "daily"},
        # 中国国债期限利差（10Y−2Y，该接口现成给这一列）。债基的「久期环境」代理：
        # 曲线越平坦，长端相对短端越没有补偿，拉久期的性价比越低（这也是 2024 年以来
        # 「久期拥挤」被反复提示的机制）。⚠️ 它是**曲线形态**代理，**不等于机构持仓久期**
        # （后者无公开日频源），note 里必须写清。
        "cn_term_spread": {"fn": "bond_zh_us_rate", "date": ["日期"],
                           "value": ["中国国债收益率10年-2年"], "period": "daily",
                           "start_param": "start_date", "lookback_days": 2200},
        # 美债 2Y（黄金台账①的姊妹因子）。2Y 比 10Y 更贴近货币政策路径，
        # 黄金实证里它对金价的负相关（−0.305）确实强于 10Y（−0.261）。
        "us2_y": {"fn": "bond_zh_us_rate", "date": ["日期"], "value": ["美国国债收益率2年"],
                  "period": "daily", "start_param": "start_date", "lookback_days": 400},
        # 美国 CPI 同比（黄金台账③「实际利率代理」的另一条腿）。
        # ⚠️ 该源最新一期「现值」常为 NaN（发布日期未到），必须靠 dropna 丢弃，
        # 否则会拿 NaN 去算实际利率、把整条因子判成数据缺失。
        "us_cpi_yoy": {"fn": "macro_usa_cpi_yoy", "date": ["时间"], "value": ["现值"],
                       "period": "monthly"},
        # 全球黄金 ETF 总库存（黄金台账④）。快变量、与金价同步/滞后正相关（r=+0.468），
        # 只能做同步确认，不能当领先指标——台账 note 里必须写明。
        # 该接口实测约 21s（金十全量 2875 行），超过全局 AKSHARE_CALL_TIMEOUT(20s)，
        # 故单独放宽——否则每次都被判超时、因子永远「数据缺失」。
        "gold_etf_holdings": {"fn": "macro_cons_gold", "date": ["日期"], "value": ["总库存"],
                              "period": "daily", "timeout": 60},
        # 中国官方黄金储备（黄金台账⑥，慢变量）。「统计时间」是 "2026.8" 点分月度格式，
        # 靠 _normalize_report_date 归一到月末。
        "cn_gold_reserve": {"fn": "macro_china_foreign_exchange_gold", "date": ["统计时间"],
                            "value": ["黄金储备"], "period": "monthly"},
        # 上海金 Au99.99 现货收盘（黄金台账的主标的与「位置维度」的基准）。
        # 该源 2016 年起，约 2370 个交易日——位置分位只用得上近 3 年窗口。
        "au_sge": {"fn": "spot_hist_sge", "date": ["date"], "value": ["close"],
                   "period": "daily", "kwargs": {"symbol": "Au99.99"}},
        # 上海银基准价（早盘价），与上海金合成金银比（黄金台账⑤）。
        # 该源起点晚于上海金（1690 行 vs 2372 行），故金银比样本更短。
        "ag_sge": {"fn": "spot_silver_benchmark_sge", "date": ["交易时间"],
                   "value": ["早盘价"], "period": "daily"},
        # ── 铜周期台账（cu_regime）──
        # 沪铜主连（新浪期货，2005 年起约 5288 个交易日）。走 calc 是因为
        # futures_main_sina 返回中文列且需由闭包固定 symbol，通用列名匹配表写不下。
        # ⚠️ 主连是主力合约拼接序列，换月处有跳价——看趋势/分位可靠，看单点绝对价位注意。
        "cu_main": {"calc": "_calc_cu_main", "period": "daily"},
        # 螺纹钢主连（新浪期货，2009 年起约 4252 个交易日）。铜周期台账④：
        # 中国地产基建需求的实物代理，实测与铜价 r=+0.361（全台账最强正相关）。
        "rb_main": {"calc": "_calc_rb_main", "period": "daily"},
        # 中国 10Y 已在上方（cn10_y）声明；铜周期台账③直接复用，不重复声明。
        # ── 猪周期台账（hog_regime）──
        # 生猪现货指数（博亚和讯，**周频**约 589 行）。四列都有用：指数(2015=100)、
        # 成交均价(元/kg)、成交均重(kg)、12个月均线。⚠️ 指数与均价是两个不同口径
        # （首跑 index=72.71 / price=10.56），台账里 ④ 用 price、⑥ 用 index÷ma12。
        # 该接口一次给出全部四列，但落库是单值长表，故拆成四个指标分别存。
        "hog_spot_index": {"fn": "index_hog_spot_price", "date": ["日期"],
                           "value": ["指数"], "period": "daily"},
        "hog_spot_price": {"fn": "index_hog_spot_price", "date": ["日期"],
                           "value": ["成交均价"], "period": "daily"},
        "hog_spot_weight": {"fn": "index_hog_spot_price", "date": ["日期"],
                            "value": ["成交均重"], "period": "daily"},
        "hog_spot_ma12": {"fn": "index_hog_spot_price", "date": ["日期"],
                          "value": ["12个月均线"], "period": "daily"},
        # 搜猪网生猪现货（元/kg，仅近 15 行——该源只给近期窗口，靠逐期累积补历史）。
        # 与 hog_spot_price 的区别：口径不同（搜猪网 vs 博亚和讯），本台账用它算
        # 猪粮比代理（搜猪网生猪 ÷ 搜猪网玉米），两条腿必须同源否则比值失真。
        "hog_lean_spot": {"fn": "spot_hog_lean_price_soozhu", "date": ["日期"],
                          "value": ["价格"], "period": "daily"},
        # 搜猪网玉米现货（元/kg，同样仅近 15 行）。猪粮比代理的分母。
        "corn_spot": {"fn": "spot_corn_price_soozhu", "date": ["日期"],
                      "value": ["价格"], "period": "daily"},
        # 生猪期货主连（大商所，元/kg，约 367 个交易日——**上市较晚**，不含 2019
        # 非瘟与 2021 猪价崩盘，故期现价差因子在台账里置信度只给 low）。
        "hog_fut_main": {"fn": "futures_hog_core", "date": ["date"],
                         "value": ["value"], "period": "daily"},
    }

    @classmethod
    def call_timeout(cls, func_name: str, *args, **kwargs) -> float | None:
        """按调用给出**外层预算**（retry 侧）；None = 用全局 FETCH_SOURCE_TIMEOUT。

        外层（retry）必须比内层（`_run_with_timeout`）宽松，否则内层还没跑完就被
        外层掐掉——症状与「源没数据」完全一样（2026-09-25 实测 gold_etf_holdings）。

        覆盖两类：
        - `ak_macro_indicator`：读 `_MACRO_APIS[*].timeout` 声明后 + 10s。
        - `ak_index_valuation`：中证指数公司估值。**内层是全局 AKSHARE_CALL_TIMEOUT(30s)，
          但外层默认 FETCH_SOURCE_TIMEOUT(20s) 反而更紧**，等于每次都要赌 20s 内跑完；
          该接口偶发慢（冷启动 + 限流等待叠加，实测 3~8s 常态、抖动可越 20s），
          一旦越线就报「所有数据源均失败」，看着像源没数据。
          2026-09-30 实测：930914/H30269 因此让股息率因子（红利策略的核心）静默缺失。
        """
        if func_name == "ak_index_valuation":
            return float(Config.AKSHARE_CALL_TIMEOUT) + 15.0
        if func_name != "ak_macro_indicator" or not args:
            return None
        api = cls._MACRO_APIS.get(args[0]) or {}
        per = api.get("timeout")
        return float(per) + 10.0 if per else None

    @classmethod
    def get_macro_indicator(cls, indicator: str) -> pd.DataFrame:
        """获取宏观指标时序，统一输出列 [report_date, value]（升序）。

        月度 "YYYY-MM" 归一为月末日期；日期列与数值列按候选表防御性匹配
        （列名漂移时抛 RuntimeError 并携带指标名与期望列，便于定位）。
        """
        api = cls._MACRO_APIS.get(indicator)
        if not api:
            raise NotImplementedError(f"未知宏观指标: {indicator}")
        ak = cls._get_ak()
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            calc = api.get("calc")
            if calc:
                # 本地计算型指标：不经 akshare 列名匹配，计算函数直接产出
                # [report_date, value]（多列原始材料在计算函数里换算成单一序列）。
                out = _MACRO_CALCS[calc]()
                if out is None or out.empty:
                    raise RuntimeError(f"宏观指标 {indicator} 无有效数据行")
                return out.sort_values("report_date").reset_index(drop=True)
            fn = getattr(ak, api["fn"])
            kwargs = _build_macro_call_kwargs(api)
            # 慢接口可声明 per-indicator timeout（如 macro_cons_gold 实测约 21s，
            # 用全局 20s 会每次都被判超时——症状是「源没有数据」，其实是超时）。
            df = _run_with_timeout(lambda: fn(**kwargs), api.get("timeout"))
            if df is None or df.empty:
                raise RuntimeError(f"akshare 未获取到宏观指标数据({indicator})")

            date_col = _match_column(df, api["date"])
            value_col = _match_column(df, api["value"])
            if date_col is None or value_col is None:
                raise RuntimeError(
                    f"宏观指标 {indicator} 列名漂移: 需要 {api['date']}/{api['value']}，"
                    f"实际列 {list(df.columns)}")
            values = pd.to_numeric(df[value_col], errors="coerce")
            # 单位换算：中行牌价按「分」计价（6.7459 报 674.59），台账阈值按元设定，
            # 故由配置声明 value_scale，避免在因子层为某个源的计量单位写特例。
            scale = api.get("value_scale")
            if scale:
                values = values * float(scale)
            out = pd.DataFrame({
                "report_date": df[date_col].astype(str).map(_normalize_report_date),
                "value": values,
            }).dropna(subset=["report_date", "value"])
            if out.empty:
                raise RuntimeError(f"宏观指标 {indicator} 无有效数据行")
            return out.sort_values("report_date").reset_index(drop=True)
        except RuntimeError:
            raise
        except NotImplementedError:
            raise
        except Exception as e:
            raise RuntimeError(f"akshare 获取宏观指标 {indicator} 失败: {e}")

    # ── 指数估值与中证指数日线 ──
    # 红利低波这类策略指数是中证指数公司编制（H 开头），既不是交易所指数
    # （stock_zh_index_daily_em 只认 sh/sz 前缀），也不在个股估值表覆盖范围内。

    @classmethod
    def get_index_valuation(cls, code: str) -> pd.DataFrame:
        """中证指数公司指数估值时序（股息率 / 市盈率）。

        code 为中证指数代码（H30269 = 中证红利低波动），非交易所代码。

        返回列（按 report_date 升序，与 get_macro_indicator 口径一致）：
            report_date / dy / pe / dy_alt / pe_alt
        - dy、pe ← 源「股息率1 / 市盈率1」，与既有红利研究口径一致
        - dy_alt、pe_alt ← 源「股息率2 / 市盈率2」（源未明示 1/2 口径差异，
          双列原样保留，不猜测含义）

        已知限制：该源只返回最近 20 个交易日，**无法直接算长期历史分位**；
        消费方需用 utils/cache/macro_db.upsert_indicator_series 逐期累积。
        """
        ak = cls._get_ak()
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN,
                                    max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = _run_with_timeout(lambda: ak.stock_zh_index_value_csindex(symbol=code))
            if df is None or df.empty:
                raise RuntimeError(f"akshare 未获取到指数估值数据({code})")

            date_col = _match_column(df, ["日期", "date"])
            if date_col is None:
                raise RuntimeError(f"指数估值 {code} 缺日期列: 实际列 {list(df.columns)}")
            dy_col = _match_column(df, ["股息率1", "股息率"])
            if dy_col is None:
                raise RuntimeError(
                    f"指数估值 {code} 列名漂移: 需要 股息率1，实际列 {list(df.columns)}")

            out = pd.DataFrame({
                "report_date": df[date_col].astype(str).map(_normalize_report_date),
                "dy": pd.to_numeric(df[dy_col], errors="coerce"),
            })
            for target, cands in (("pe", ["市盈率1", "市盈率"]),
                                  ("dy_alt", ["股息率2"]),
                                  ("pe_alt", ["市盈率2"])):
                col = _match_column(df, cands)
                if col is not None:
                    out[target] = pd.to_numeric(df[col], errors="coerce")

            # 源为日期倒序（最新在前），统一转升序；同日重复保留更完整的一行
            out = out.dropna(subset=["report_date", "dy"])
            out = out.drop_duplicates(subset=["report_date"], keep="last")
            if out.empty:
                raise RuntimeError(f"指数估值 {code} 无有效数据行")
            return out.sort_values("report_date").reset_index(drop=True)
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"akshare 获取指数估值 {code} 失败: {e}")

    @classmethod
    def get_index_hist_csindex(cls, code: str, start: str = "", end: str = "") -> pd.DataFrame:
        """中证指数公司指数日线（点位，含滚动市盈率）。

        输出列: report_date / open / high / low / close / volume / amount
                / pct_change / change_amount / pe_ttm
        中证指数发布的是点位而非可交易价格，无复权概念，故不做复权处理。

        为什么不用 stock_zh_index_daily_em：该接口只认交易所指数
        （sh000015 等），传中证代码（H30269）返回空表。
        """
        ak = cls._get_ak()
        from datetime import date as _date
        s = (start or "20100101").replace("-", "")
        e = (end or _date.today().strftime("%Y%m%d")).replace("-", "")
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN,
                                    max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = _run_with_timeout(
                lambda: ak.stock_zh_index_hist_csindex(symbol=code, start_date=s, end_date=e))
            if df is None or df.empty:
                raise RuntimeError(f"akshare 未获取到中证指数日线({code} {s}-{e})")

            date_col = _match_column(df, ["日期", "date"])
            if date_col is None:
                raise RuntimeError(f"中证指数日线 {code} 缺日期列: 实际列 {list(df.columns)}")
            out = pd.DataFrame({
                "report_date": df[date_col].astype(str).map(_normalize_report_date)})
            for target, cands in (("open", ["开盘", "open"]),
                                  ("high", ["最高", "high"]),
                                  ("low", ["最低", "low"]),
                                  ("close", ["收盘", "close"]),
                                  ("volume", ["成交量", "volume"]),
                                  ("amount", ["成交金额", "成交额", "amount"]),
                                  ("pct_change", ["涨跌幅", "pct_change"]),
                                  ("change_amount", ["涨跌", "change_amount"]),
                                  ("pe_ttm", ["滚动市盈率"])):
                col = _match_column(df, cands)
                if col is not None:
                    out[target] = pd.to_numeric(df[col], errors="coerce")

            if "close" not in out.columns:
                raise RuntimeError(
                    f"中证指数日线 {code} 缺收盘列: 实际列 {list(df.columns)}")
            out = out.dropna(subset=["report_date", "close"])
            if out.empty:
                raise RuntimeError(f"中证指数日线 {code} 无有效数据行")
            return out.sort_values("report_date").reset_index(drop=True)
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"akshare 获取中证指数日线 {code} 失败: {e}")

    @classmethod
    def get_margin_detail(cls, market: str = "sh", date: str = "") -> pd.DataFrame:
        """获取融资融券个股明细"""
        ak = cls._get_ak()
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            if market == "sh":
                df = ak.stock_margin_detail_sse(date=date)
            else:
                df = ak.stock_margin_detail_szse(date=date)
            if df is None or df.empty:
                raise RuntimeError(f"akshare 未获取到融资融券明细数据({market}/{date})")
            return df
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"akshare 获取融资融券明细失败: {e}")

    # ── 大宗交易 ──

    @classmethod
    def get_block_trades(cls, symbol: str = "A股", start_date: str = "", end_date: str = "") -> pd.DataFrame:
        """获取大宗交易每日明细"""
        ak = cls._get_ak()
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_dzjy_mrmx(symbol=symbol, start_date=start_date, end_date=end_date)
            if df is None or df.empty:
                raise RuntimeError(f"akshare 未获取到大宗交易明细数据({symbol})")
            return df
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"akshare 获取大宗交易明细失败: {e}")

    @classmethod
    def get_block_trade_stats(cls, start_date: str = "", end_date: str = "") -> pd.DataFrame:
        """获取大宗交易每日统计"""
        ak = cls._get_ak()
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_dzjy_mrtj(start_date=start_date, end_date=end_date)
            if df is None or df.empty:
                raise RuntimeError(f"akshare 未获取到大宗交易统计数据({start_date}-{end_date})")
            return df
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"akshare 获取大宗交易统计失败: {e}")

    # ── 板块轮动 ──

    @classmethod
    def get_board_industry_spot(cls, symbol: str = "行业板块") -> pd.DataFrame:
        """获取板块实时行情"""
        ak = cls._get_ak()
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_board_industry_spot_em(symbol=symbol)
            if df is None or df.empty:
                raise RuntimeError(f"akshare 未获取到板块实时行情数据({symbol})")
            return df
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"akshare 获取板块实时行情失败: {e}")

    @classmethod
    def get_board_industry_hist(cls, symbol: str, period: str = "daily", start: str = "", end: str = "") -> pd.DataFrame:
        """获取板块历史K线"""
        ak = cls._get_ak()
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_board_industry_hist_em(symbol=symbol, period=period, start_date=start, end_date=end)
            if df is None or df.empty:
                raise RuntimeError(f"akshare 未获取到板块历史K线数据({symbol})")
            return df
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"akshare 获取板块历史K线失败: {e}")

    # ── 板块排名/热点 ──

    @classmethod
    def get_sector_rankings(cls, n: int = 5) -> tuple:
        """
        获取行业板块涨跌排名

        Returns:
            (涨幅前n, 跌幅前n) 的元组
        """
        ak = cls._get_ak()

        # 优先东财接口
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_board_industry_name_em()
            if df is not None and not df.empty:
                return cls._calc_rankings(df, '涨跌幅', '板块名称', n)
        except Exception as e:
            logger.warning(f"[akshare] 东财接口获取行业板块排名失败: {e}，尝试新浪接口")

        # 新浪接口
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_sector_spot(indicator='行业')
            if df is not None and not df.empty:
                return cls._calc_rankings(df, '涨跌幅', '板块', n)
        except Exception as e:
            logger.warning(f"[akshare] 新浪接口获取行业板块排名失败: {e}")

        raise RuntimeError("akshare 获取行业板块排名失败")

    @classmethod
    def get_concept_rankings(cls, n: int = 5) -> tuple:
        """获取概念板块涨跌排名"""
        ak = cls._get_ak()

        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_board_concept_name_em()
            if df is not None and not df.empty:
                return cls._calc_rankings(df, '涨跌幅', '板块名称', n)
        except Exception as e:
            logger.warning(f"[akshare] 获取概念板块排名失败: {e}")

        raise RuntimeError("akshare 获取概念板块排名失败")

    @classmethod
    def _calc_rankings(cls, df, change_col, name_col, n):
        """计算涨跌排名"""
        import pandas as pd

        df = df.copy()
        df[change_col] = pd.to_numeric(df[change_col], errors='coerce')
        df = df.dropna(subset=[change_col])

        # 涨幅前n
        top = df.nlargest(n, change_col)
        top_list = [
            {'name': str(row[name_col]), 'change_pct': float(row[change_col])}
            for _, row in top.iterrows()
        ]

        # 跌幅前n
        bottom = df.nsmallest(n, change_col)
        bottom_list = [
            {'name': str(row[name_col]), 'change_pct': float(row[change_col])}
            for _, row in bottom.iterrows()
        ]

        return (top_list, bottom_list)

    @classmethod
    def get_hot_stocks(cls, n: int = 10) -> list:
        """获取热点股票（人气榜）"""
        ak = cls._get_ak()

        # 尝试东财人气榜
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_hot_rank_em()
            if df is not None and not df.empty:
                rows = []
                for _, row in df.head(n).iterrows():
                    rows.append({
                        'rank': int(row.get('当前排名', 0)),
                        'code': str(row.get('代码', '')).strip(),
                        'name': str(row.get('股票名称', '')).strip(),
                        'price': float(row.get('最新价', 0) or 0),
                        'change_pct': float(row.get('涨跌幅', 0) or 0),
                        'source': '东方财富人气榜',
                    })
                return rows
        except Exception as e:
            logger.warning(f"[akshare] 获取东财人气榜失败: {e}")

        # 尝试东财飙升榜
        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_hot_up_em()
            if df is not None and not df.empty:
                rows = []
                for _, row in df.head(n).iterrows():
                    rows.append({
                        'rank': len(rows) + 1,
                        'code': str(row.get('代码', '')).strip(),
                        'name': str(row.get('股票名称', '')).strip(),
                        'price': float(row.get('最新价', 0) or 0),
                        'change_pct': float(row.get('涨跌幅', 0) or 0),
                        'source': '东方财富飙升榜',
                    })
                return rows
        except Exception as e:
            logger.warning(f"[akshare] 获取东财飙升榜失败: {e}")

        raise RuntimeError("akshare 获取热点股票失败")

    @classmethod
    def get_limit_up_pool(cls, date: str = None, n: int = 20) -> list:
        """获取涨停池"""
        from datetime import datetime
        import pandas as pd

        ak = cls._get_ak()
        query_date = date or datetime.now().strftime('%Y%m%d')

        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_zt_pool_em(date=query_date)
            if df is None or df.empty:
                return []

            df = df.copy()
            # 数值列转换
            for col in ('连板数', '封板资金', '成交额', '换手率', '涨跌幅'):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')

            # 按连板数排序
            if '连板数' in df.columns:
                df = df.sort_values('连板数', ascending=False)

            rows = []
            for _, row in df.head(n).iterrows():
                rows.append({
                    'code': str(row.get('代码', '')).strip(),
                    'name': str(row.get('名称', '')).strip(),
                    'change_pct': float(row.get('涨跌幅', 0) or 0),
                    'price': float(row.get('最新价', 0) or 0),
                    'amount': float(row.get('成交额', 0) or 0),
                    'turnover_rate': float(row.get('换手率', 0) or 0),
                    'seal_amount': float(row.get('封板资金', 0) or 0),
                    'consecutive_boards': int(row.get('连板数', 0) or 0),
                    'industry': str(row.get('所属行业', '')).strip(),
                })
            return rows
        except Exception as e:
            logger.warning(f"[akshare] 获取涨停池失败: {e}")
            return []

    # ── 筹码分布 ──

    @classmethod
    def get_chip_distribution(cls, symbol: str) -> dict:
        """获取筹码分布"""
        ak = cls._get_ak()

        # 只支持 A 股
        market = DataSourceManager.detect_market(symbol)
        if market in ("hk", "us", "etf"):
            raise RuntimeError(f"akshare 不支持{market}市场的筹码分布")

        try:
            cls._enforce_rate_limit(min_seconds=Config.AKSHARE_RATE_LIMIT_MIN, max_seconds=Config.AKSHARE_RATE_LIMIT_MAX)
            df = ak.stock_cyq_em(symbol=symbol)
            if df is None or df.empty:
                raise RuntimeError(f"akshare 未获取到 {symbol} 的筹码分布数据")

            # 取最新一天的数据
            latest = df.iloc[-1]

            return {
                'code': symbol,
                'date': str(latest.get('日期', '')),
                'profit_ratio': float(latest.get('获利比例', 0) or 0),
                'avg_cost': float(latest.get('平均成本', 0) or 0),
                'cost_90_low': float(latest.get('90成本-低', 0) or 0),
                'cost_90_high': float(latest.get('90成本-高', 0) or 0),
                'concentration_90': float(latest.get('90集中度', 0) or 0),
                'cost_70_low': float(latest.get('70成本-低', 0) or 0),
                'cost_70_high': float(latest.get('70成本-高', 0) or 0),
                'concentration_70': float(latest.get('70集中度', 0) or 0),
            }
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"akshare 获取 {symbol} 筹码分布失败: {e}")
