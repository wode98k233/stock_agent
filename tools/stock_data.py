"""
选股雷达 - 股票数据工具
提供板块查询、个股行情、历史K线、新闻、评级、财务数据
所有请求走分类缓存 + 重试机制
"""
import logging
import pandas as pd
from datetime import datetime, timedelta
from tools.fetcher import (
    ak_stock_hist, ak_spot_em,
    ak_board_industry_cons, ak_board_concept_cons,
    ak_board_industry_list, ak_board_concept_list,
    ak_stock_news, ak_stock_rating, ak_financial_abstract,
)
from utils.cache import (
    get_history_cache, set_history_cache,
    get_board_cache, set_board_cache,
    get_news_cache, set_news_cache,
    get_rating_cache, set_rating_cache,
    get_financial_cache, set_financial_cache,
    get_board_list_cache, set_board_list_cache,
    get_realtime_cache, set_realtime_cache,
    is_market_closed,
)


def _get_logger(logger):
    if logger is not None:
        return logger
    from utils.logger import get_child_logger
    return get_child_logger("stock_data")


# ── 板块成分股 ──────────────────────────────────────────────

def get_board_stocks(board_name: str, logger=None) -> pd.DataFrame:
    """获取板块/概念的成分股列表"""
    logger = _get_logger(logger)
    cached = get_board_cache(board_name)
    if cached is not None:
        logger.info(f"缓存命中 板块成分股: {board_name} ({len(cached)}只)")
        return cached

    logger.info(f"请求板块成分股: {board_name}")
    try:
        df = ak_board_industry_cons(board_name)
    except Exception:
        try:
            df = ak_board_concept_cons(board_name)
        except Exception as e:
            raise ValueError(f"未找到板块/概念: {board_name}。错误: {e}")

    set_board_cache(board_name, df)
    return df


# ── 个股历史K线 ──────────────────────────────────────────────

def get_stock_history(symbol: str, days: int = 120, logger=None) -> pd.DataFrame:
    """
    获取个股历史K线，默认最近120天
    返回含: 日期, 开盘, 收盘, 最高, 最低, 成交量, 成交额, 振幅, 涨跌幅, 涨跌额, 换手率
    """
    logger = _get_logger(logger)
    end = datetime.now().strftime('%Y%m%d')
    start = (datetime.now() - timedelta(days=days + 60)).strftime('%Y%m%d')  # 多取一些节假日

    cached = get_history_cache(symbol, start, end)
    if cached is not None:
        logger.info(f"缓存命中 历史K线: {symbol} ({len(cached)}条)")
        return cached

    logger.info(f"请求历史K线: {symbol}")
    df = ak_stock_hist(symbol, period="daily", start=start, end=end)

    if df.empty:
        raise ValueError(f"未获取到 {symbol} 的历史数据")

    # 标准化列名
    col_map = {
        '日期': 'date', '开盘': 'open', '收盘': 'close',
        '最高': 'high', '最低': 'low', '成交量': 'volume',
        '成交额': 'amount', '振幅': 'amplitude',
        '涨跌幅': 'pct_chg', '涨跌额': 'chg', '换手率': 'turnover_rate',
    }
    df = df.rename(columns=col_map)
    df['date'] = pd.to_datetime(df['date'])
    df = df.set_index('date').sort_index()
    # 只取需要的天数
    df = df.tail(days)

    set_history_cache(symbol, start, end, df)
    return df


# ── 实时行情（单只股票） ─────────────────────────────────────

def get_stock_realtime(symbol: str, logger=None) -> dict:
    """
    获取单只股票的实时行情快照
    返回: code, name, price, open, high, low, volume, amount, pct_chg, chg, turnover_rate 等
    """
    logger = _get_logger(logger)
    cached = get_realtime_cache(symbol)
    if cached is not None:
        logger.info(f"缓存命中 实时行情: {symbol}")
        return cached

    logger.info(f"请求实时行情: {symbol}")
    df = ak_spot_em()
    row = df[df['代码'] == symbol]
    if row.empty:
        raise ValueError(f"未找到股票: {symbol} \n原始数据: {df}")
    row = row.iloc[0]

    data = {
        'code': symbol,
        'name': row.get('名称', ''),
        'price': float(row.get('最新价', 0)),
        'open': float(row.get('今开', 0)),
        'high': float(row.get('最高', 0)),
        'low': float(row.get('最低', 0)),
        'pre_close': float(row.get('昨收', 0)),
        'volume': int(row.get('成交量', 0)),
        'amount': float(row.get('成交额', 0)),
        'pct_chg': float(row.get('涨跌幅', 0)),
        'chg': float(row.get('涨跌额', 0)),
        'turnover_rate': float(row.get('换手率', 0)),
        'amplitude': float(row.get('振幅', 0)),
        'volume_ratio': float(row.get('量比', 0)),
        'pe': float(row.get('市盈率-动态', 0) or 0),
        'pb': float(row.get('市净率', 0) or 0),
        'total_mv': float(row.get('总市值', 0) or 0),
    }
    set_realtime_cache(symbol, data)
    return data


# ── 批量实时行情 ─────────────────────────────────────────────

def get_batch_realtime(symbols: list, logger=None) -> dict:
    """批量获取实时行情，一次请求全部A股后过滤，减少请求次数"""
    logger = _get_logger(logger)
    result = {}
    missing = []
    for s in symbols:
        cached = get_realtime_cache(s)
        if cached:
            result[s] = cached
        else:
            missing.append(s)

    if not missing:
        return result

    logger.info(f"请求批量实时行情: {len(missing)}只")
    df = ak_spot_em()
    for s in missing:
        row = df[df['代码'] == s]
        if not row.empty:
            r = row.iloc[0]
            data = {
                'code': s,
                'name': r.get('名称', ''),
                'price': float(r.get('最新价', 0)),
                'open': float(r.get('今开', 0)),
                'high': float(r.get('最高', 0)),
                'low': float(r.get('最低', 0)),
                'pre_close': float(r.get('昨收', 0)),
                'volume': int(r.get('成交量', 0)),
                'amount': float(r.get('成交额', 0)),
                'pct_chg': float(r.get('涨跌幅', 0)),
                'chg': float(r.get('涨跌额', 0)),
                'turnover_rate': float(r.get('换手率', 0)),
                'amplitude': float(r.get('振幅', 0)),
                'volume_ratio': float(r.get('量比', 0)),
                'pe': float(r.get('市盈率-动态', 0) or 0),
                'pb': float(r.get('市净率', 0) or 0),
                'total_mv': float(r.get('总市值', 0) or 0),
            }
            result[s] = data
            set_realtime_cache(s, data)
    return result


# ── 新闻 ────────────────────────────────────────────────────

def get_stock_news(symbol: str, limit: int = 10, logger=None) -> list:
    """获取个股最新新闻"""
    logger = _get_logger(logger)
    cached = get_news_cache(symbol)
    if cached is not None:
        logger.info(f"缓存命中 新闻: {symbol} ({len(cached)}条)")
        return cached

    logger.info(f"请求新闻: {symbol}")
    try:
        df = ak_stock_news(symbol)
        news = []
        for _, row in df.head(limit).iterrows():
            # 尝试多种列名组合
            title = str(row.get('新闻标题', '') or row.get('标题', '') or row.get('title', '')).strip()
            time_str = str(row.get('发布时间', '') or row.get('时间', '') or row.get('datetime', '')).strip()
            content = str(row.get('新闻内容', '') or row.get('内容', '') or row.get('content', '')).strip()
            source = str(row.get('文章来源', '') or row.get('来源', '') or row.get('source', '')).strip()
            
            item = {
                'title': title,
                'time': time_str,
                'content': content,
                'source': source,
            }
            # 只保留有有效标题的新闻
            if item['title']:
                news.append(item)
        
        # 只有在有有效新闻的情况下才写入缓存
        if news:
            set_news_cache(symbol, news)
            logger.info(f"获取到有效新闻: {symbol} ({len(news)}条)")
        else:
            logger.warning(f"未获取到有效新闻: {symbol}")
        
        return news
    except Exception as e:
        logger.error(f"获取新闻失败: {symbol}, {e}")
        return []


# ── 机构评级 ────────────────────────────────────────────────

def get_stock_rating(symbol: str, logger=None) -> dict:
    """获取个股机构评级（优先使用千股千评）"""
    logger = _get_logger(logger)
    
    cached = get_rating_cache(symbol)
    if cached is not None:
        logger.info(f"缓存命中 评级: {symbol}")
        return cached

    logger.info(f"请求机构评级: {symbol}")
    try:
        df = ak_stock_rating(symbol)
        if df.empty:
            return {'rating': '无评级', 'target_price': None, 'count_90d': 0, 'summary': '暂无机构评级数据'}

        latest = df.iloc[0]
        
        # 检查是否是千股千评的数据（有综合得分）
        if '综合得分' in df.columns and '名称' in df.columns:
            # 千股千评数据
            name = str(latest.get('名称', ''))
            
            # 提取所有可用的千股千评信息
            def get_numeric_val(key, default=0.0, round_digits=2):
                val = latest.get(key, default)
                try:
                    return round(float(val), round_digits)
                except:
                    return default
            
            score = get_numeric_val('综合得分')
            latest_price = get_numeric_val('最新价')
            pct_chg = get_numeric_val('涨跌幅')
            turnover = get_numeric_val('换手率')
            pe = get_numeric_val('市盈率')
            main_cost = get_numeric_val('主力成本')
            inst_part = get_numeric_val('机构参与度')
            up = get_numeric_val('上升', round_digits=0)
            rank = get_numeric_val('目前排名', round_digits=0)
            focus_index = get_numeric_val('关注指数')
            trade_date = str(latest.get('交易日', ''))
            
            # 计算溢价
            premium = 0.0
            if latest_price and main_cost and main_cost > 0:
                premium = round((latest_price - main_cost) / main_cost * 100, 2)
            
            result = {
                'rating': f"{name} 综合得分{score}",
                'target_price': None,
                'count_90d': 0,
                'summary': f"{name} 最新价{latest_price}，综合得分{score}，主力成本{main_cost}，溢价{premium}%，机构参与度{inst_part}%，排名{int(rank)}",
                'rating_summary': f"{name} 最新价{latest_price}，综合得分{score}，主力成本{main_cost}，溢价{premium}%，机构参与度{inst_part}%，排名{int(rank)}",
                'name': name,
                'score': score,
                'latest_price': latest_price,
                'pct_chg': pct_chg,
                'turnover': turnover,
                'pe': pe,
                'main_cost': main_cost,
                'premium': premium,
                'institution_participation': inst_part,
                'up': int(up),
                'rank': int(rank),
                'focus_index': focus_index,
                'trade_date': trade_date,
            }
        else:
            # 原有的机构评级数据
            count_90d = 0
            cutoff_date = datetime.now() - timedelta(days=90)
            
            if '评级日期' in df.columns:
                try:
                    df['评级日期'] = pd.to_datetime(df['评级日期'])
                    recent = df[df['评级日期'] >= cutoff_date]
                    count_90d = len(recent)
                except:
                    count_90d = len(df)
            else:
                count_90d = len(df)

            rating = ""
            target_price = None
            
            for col in ['评级', '综合评级', '投资评级']:
                if col in df.columns:
                    rating = str(latest.get(col, ''))
                    break
            
            for col in ['目标价', '平均目标价']:
                if col in df.columns:
                    try:
                        val = latest.get(col, 0)
                        if val and str(val).strip():
                            target_price = round(float(val), 2)
                    except:
                        pass
                    break

            result = {
                'rating': rating or '无评级',
                'target_price': target_price,
                'count_90d': count_90d,
                'summary': f"近90天{count_90d}个机构评级，最新: {rating or '无'}，目标价: {target_price or 'N/A'}",
                'rating_summary': f"近90天{count_90d}个机构评级，最新: {rating or '无'}，目标价: {target_price or 'N/A'}",
            }
        
        # 调用 cache.py 里的 set_rating_cache，它自己去判断缓存策略
        set_rating_cache(symbol, result)
        
        return result
    except Exception as e:
        logger.error(f"获取评级失败: {symbol}, {e}")
        return {'rating': '获取失败', 'target_price': None, 'count_90d': 0, 'summary': f'获取评级失败: {str(e)}'}


# ── 财务摘要 ────────────────────────────────────────────────

def get_stock_financial(symbol: str, logger=None) -> dict:
    """获取个股核心财务指标，附带解读"""
    logger = _get_logger(logger)
    cached = get_financial_cache(symbol)
    if cached is not None:
        logger.info(f"缓存命中 财务: {symbol}")
        return cached

    logger.info(f"请求财务数据: {symbol}")
    try:
        df = ak_financial_abstract(symbol)
        if df.empty:
            return {}
        # 取最新一期
        latest = df.iloc[0].to_dict()
        # 转换类型
        result = {}
        for k, v in latest.items():
            try:
                result[k] = float(v) if v is not None else 0
            except (ValueError, TypeError):
                result[k] = str(v)

        # 添加解读字段
        interpretation = _build_financial_interpretation(result)
        if interpretation:
            result['_interpretation'] = interpretation

        set_financial_cache(symbol, result)
        return result
    except Exception as e:
        logger.error(f"获取财务数据失败: {symbol}, {e}")
        return {}


def _build_financial_interpretation(data: dict) -> dict:
    """根据财务数据生成阈值解读"""
    interp = {}

    # ROE — 尝试多种可能的字段名
    roe = _find_numeric(data, ['roeAvg', 'roe', 'ROE', '净资产收益率'])
    if roe is not None:
        level = '优秀' if roe > 15 else '良好' if roe > 10 else '一般' if roe > 5 else '偏低'
        interp['roe'] = {'value': round(roe, 2), 'level': level}

    # 毛利率
    gm = _find_numeric(data, ['grossProfitMargin', 'gross_margin', '毛利率', '销售毛利率'])
    if gm is not None:
        level = '高毛利' if gm > 40 else '中等' if gm > 20 else '低毛利'
        interp['gross_margin'] = {'value': round(gm, 2), 'level': level}

    # 资产负债率
    dr = _find_numeric(data, ['debtRatio', 'debt_ratio', '资产负债率'])
    if dr is not None:
        level = '安全' if dr < 50 else '适中' if dr < 70 else '偏高'
        interp['debt_ratio'] = {'value': round(dr, 2), 'level': level}

    # 营收增长率
    rg = _find_numeric(data, ['revenueYoY', 'revenue_yoy', '营收同比', '营业收入增长率'])
    if rg is not None:
        level = '高增长' if rg > 20 else '稳定' if rg > 0 else '下滑'
        interp['revenue_growth'] = {'value': round(rg, 2), 'level': level}

    # 净利润增长率
    npg = _find_numeric(data, ['netProfitYoY', 'net_profit_yoy', '净利润同比', '净利润增长率'])
    if npg is not None:
        level = '高增长' if npg > 20 else '稳定' if npg > 0 else '下滑'
        interp['profit_growth'] = {'value': round(npg, 2), 'level': level}

    # 市盈率
    pe = _find_numeric(data, ['pe', 'PE', '市盈率', '市盈率(动)'])
    if pe is not None:
        if pe < 0:
            level = '亏损'
        elif pe < 15:
            level = '低估'
        elif pe < 30:
            level = '合理'
        elif pe < 60:
            level = '偏高'
        else:
            level = '高估'
        interp['pe'] = {'value': round(pe, 2), 'level': level}

    return interp


def _find_numeric(data: dict, candidates: list):
    """从 dict 中按候选字段名查找第一个有效的数值"""
    for key in candidates:
        if key in data:
            val = data[key]
            if isinstance(val, (int, float)):
                return val
            try:
                return float(val)
            except (ValueError, TypeError):
                continue
    return None


# ── 板块/概念列表 ───────────────────────────────────────────

def get_industry_list(logger=None) -> pd.DataFrame:
    logger = _get_logger(logger)
    cached = get_board_list_cache('industry')
    if cached is not None:
        return cached
    df = ak_board_industry_list()
    set_board_list_cache('industry', df)
    return df


def get_concept_list(logger=None) -> pd.DataFrame:
    logger = _get_logger(logger)
    cached = get_board_list_cache('concept')
    if cached is not None:
        return cached
    df = ak_board_concept_list()
    set_board_list_cache('concept', df)
    return df