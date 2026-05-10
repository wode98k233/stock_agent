"""新浪财经直连数据源（纯HTTP，无第三方依赖）"""
import time
import json
import logging
import requests
import pandas as pd
import os
from datetime import datetime, timedelta
from utils.app_paths import get_data_path
from .base import DataSource
from .config import Config

logger = logging.getLogger("radar.fetcher")


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _safe_int(v, default=0):
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return default


class SinaDirectDataSource(DataSource):
    name: str = "sina_direct"
    priority: int = 95

    _HEADERS = {
        'Referer': 'https://finance.sina.com.cn',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }

    # 缓存节点树
    _nodes_cache = None

    @classmethod
    def is_available(cls) -> bool:
        if not cls.enabled:
            return False
        try:
            r = requests.get(
                "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount",
                params={'node': 'hs_a'},
                headers=cls._HEADERS, timeout=5
            )
            return r.status_code == 200
        except:
            return False

    @classmethod
    def _sina_code(cls, symbol):
        from .base import _standardize_stock_code
        return _standardize_stock_code(symbol)

    @classmethod
    def _load_nodes(cls):
        """加载并缓存全部板块节点（从本地 industry.json 读取）"""
        if cls._nodes_cache is not None:
            return cls._nodes_cache

        industry_file = os.path.join(get_data_path(), 'symbol_data', 'industry.json')
        
        nodes = {'industry': {}, 'concept': {}}
        
        try:
            if os.path.exists(industry_file):
                with open(industry_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                def walk(tree):
                    if isinstance(tree, list):
                        if len(tree) >= 3 and isinstance(tree[2], str):
                            code = tree[2]
                            name = tree[0] if isinstance(tree[0], str) else ''
                            if code.startswith('new_'):
                                nodes['industry'][name] = code
                            elif code.startswith('chgn_') or code.startswith('gn_'):
                                nodes['concept'][name] = code
                        for item in tree:
                            walk(item)
                
                walk(data)
                logger.info(f"新浪板块节点已从本地缓存加载: 行业{len(nodes['industry'])}个, 概念{len(nodes['concept'])}个")
            else:
                logger.warning(f"本地板块缓存文件不存在: {industry_file}")
        except Exception as e:
            logger.error(f"读取本地板块缓存失败: {e}, 将从网络获取")
            # 降级：从网络获取
            try:
                r = requests.get(
                    'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodes',
                    headers=cls._HEADERS, timeout=Config.REQUEST_TIMEOUT
                )
                data = json.loads(r.text)

                def walk(tree):
                    if isinstance(tree, list):
                        if len(tree) >= 3 and isinstance(tree[2], str):
                            code = tree[2]
                            name = tree[0] if isinstance(tree[0], str) else ''
                            if code.startswith('new_'):
                                nodes['industry'][name] = code
                            elif code.startswith('gn_'):
                                nodes['concept'][name] = code
                        for item in tree:
                            walk(item)

                walk(data)
                logger.info(f"新浪板块节点已从网络获取: 行业{len(nodes['industry'])}个, 概念{len(nodes['concept'])}个")
            except Exception as e2:
                logger.error(f"从网络获取板块节点也失败: {e2}")

        cls._nodes_cache = nodes
        return nodes

    # ── K线 ──────────────────────────────────────────────

    @classmethod
    def get_stock_hist(cls, symbol, period="daily", start="", end=""):
        """
        获取历史K线。
        新浪K线API已废弃，降级使用腾讯财经K线API（web.ifzq.gtimg.cn），纯HTTP无依赖。
        """
        from .base import _fetch_tencent_kline
        code = cls._sina_code(symbol)
        return _fetch_tencent_kline(code, period, start, end, cls._HEADERS)

    # ── 实时行情 ─────────────────────────────────────────

    @classmethod
    def get_spot_em(cls):
        """新浪大盘行情（分页获取）"""
        try:
            r = requests.get(
                'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount',
                params={'node': 'hs_a'},
                headers=cls._HEADERS, timeout=Config.REQUEST_TIMEOUT
            )
            total = _safe_int(r.text.strip().strip('"'), 10000) if r.ok else 10000

            all_dfs = []
            page = 1
            per_page = 100
            while (page - 1) * per_page < total:
                r = requests.get(
                    'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData',
                    params={'node': 'hs_a', 'page': page, 'num': per_page, 'sort': 'symbol', 'asc': 1},
                    headers=cls._HEADERS, timeout=Config.REQUEST_TIMEOUT
                )
                if not r.ok:
                    break
                data = json.loads(r.text)
                if not data:
                    break
                rows = [{
                    '代码': item.get('code', ''),
                    '名称': item.get('name', ''),
                    '最新价': _safe_float(item.get('trade')),
                    '涨跌额': _safe_float(item.get('pricechange')),
                    '涨跌幅': _safe_float(item.get('changepercent')),
                    '昨收': _safe_float(item.get('settlement')),
                    '开盘': _safe_float(item.get('open')),
                    '最高': _safe_float(item.get('high')),
                    '最低': _safe_float(item.get('low')),
                    '成交量': _safe_int(item.get('volume')),
                    '成交额': _safe_float(item.get('amount')),
                } for item in data]
                all_dfs.append(pd.DataFrame(rows))
                page += 1
                time.sleep(0.3)

            if all_dfs:
                return pd.concat(all_dfs, ignore_index=True)
        except Exception as e:
            logger.error(f"新浪获取行情失败: {e}")
        return pd.DataFrame()

    # ── 板块（行业 + 概念）────────────────────────────────

    @classmethod
    def get_board_industry_list(cls):
        """行业板块列表（新浪行业）"""
        nodes = cls._load_nodes()
        if not nodes['industry']:
            return pd.DataFrame()
        rows = [{'板块名称': name, '板块代码': code} for name, code in nodes['industry'].items()]
        return pd.DataFrame(rows).sort_values('板块名称').reset_index(drop=True)

    @classmethod
    def get_board_concept_list(cls):
        """概念板块列表"""
        nodes = cls._load_nodes()
        if not nodes['concept']:
            return pd.DataFrame()
        rows = [{'板块名称': name, '板块代码': code} for name, code in nodes['concept'].items()]
        return pd.DataFrame(rows).sort_values('板块名称').reset_index(drop=True)

    @classmethod
    def _get_board_cons_by_code(cls, board_code):
        """通过板块代码获取成分股"""
        try:
            # 先拿总数
            r_cnt = requests.get(
                'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount',
                params={'node': board_code},
                headers=cls._HEADERS, timeout=Config.REQUEST_TIMEOUT
            )
            total = _safe_int(r_cnt.text.strip().strip('"'), 0)
            if total <= 0:
                return pd.DataFrame()

            r = requests.get(
                'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData',
                params={'node': board_code, 'page': 1, 'num': max(total, 500), 'sort': 'symbol', 'asc': 1},
                headers=cls._HEADERS, timeout=Config.REQUEST_TIMEOUT
            )
            if r.status_code != 200:
                return pd.DataFrame()
            data = json.loads(r.text)
            if not data:
                return pd.DataFrame()

            rows = [{
                '代码': item.get('code', ''),
                '名称': item.get('name', ''),
                '最新价': _safe_float(item.get('trade')),
                '涨跌幅': _safe_float(item.get('changepercent')),
                '涨跌额': _safe_float(item.get('pricechange')),
                '成交量': _safe_int(item.get('volume')),
                '成交额': _safe_float(item.get('amount')),
            } for item in data]
            return pd.DataFrame(rows)
        except Exception as e:
            logger.error(f"新浪获取板块成分股失败: {e}")
            return pd.DataFrame()

    @classmethod
    def get_board_industry_cons(cls, symbol):
        """行业板块成分股 — symbol 为板块名称或板块代码"""
        nodes = cls._load_nodes()
        # 先按名称查找
        board_code = nodes['industry'].get(symbol)
        if not board_code:
            # 如果传入的直接就是代码
            if symbol.startswith('new_'):
                board_code = symbol
            else:
                # 模糊匹配
                for name, code in nodes['industry'].items():
                    if symbol in name:
                        board_code = code
                        break
        if not board_code:
            return pd.DataFrame()
        return cls._get_board_cons_by_code(board_code)

    @classmethod
    def get_board_concept_cons(cls, symbol):
        """概念板块成分股"""
        nodes = cls._load_nodes()
        board_code = nodes['concept'].get(symbol)
        if not board_code:
            if symbol.startswith('gn_'):
                board_code = symbol
            else:
                for name, code in nodes['concept'].items():
                    if symbol in name:
                        board_code = code
                        break
        if not board_code:
            return pd.DataFrame()
        return cls._get_board_cons_by_code(board_code)

    # ── 新闻 ─────────────────────────────────────────────

    @classmethod
    def get_stock_news(cls, symbol):
        """个股新闻（从新浪财经页面解析）"""
        s = symbol.strip()
        sina_code = cls._sina_code(symbol)
        try:
            from bs4 import BeautifulSoup

            r = requests.get(
                f'https://vip.stock.finance.sina.com.cn/corp/go.php/vCB_AllNewsStock/symbol/{sina_code}.phtml',
                headers=cls._HEADERS, timeout=Config.REQUEST_TIMEOUT
            )
            if r.status_code != 200:
                return pd.DataFrame()

            soup = BeautifulSoup(r.text, 'html.parser')
            news_items = []

            # 从页面中提取新闻链接
            for a in soup.find_all('a', href=True):
                href = a.get('href', '')
                title = a.text.strip()
                # 过滤有效新闻链接
                if title and len(title) > 5 and ('/doc-' in href or '/a/' in href):
                    # 尝试从 URL 提取日期
                    import re
                    date_match = re.search(r'/(\d{4}-\d{2}-\d{2})/', href)
                    pub_date = date_match.group(1) if date_match else ''
                    news_items.append({
                        '标题': title,
                        '时间': pub_date,
                        '内容': '',
                        '链接': href,
                        '关键词': s,
                    })

            if news_items:
                return pd.DataFrame(news_items)
        except Exception as e:
            logger.error(f"新浪获取新闻失败: {e}")
        return pd.DataFrame()

    # ── 不支持的功能 ──────────────────────────────────────

    @classmethod
    def get_stock_rating(cls, symbol):
        raise NotImplementedError("新浪财经 不支持机构评级")

    @classmethod
    def get_financial_abstract(cls, symbol):
        raise NotImplementedError("新浪财经 不支持财务摘要")

    @classmethod
    def get_valuation_indicators(cls, symbol: str) -> pd.DataFrame:
        """通过新浪财经获取个股估值指标"""
        code = cls._sina_code(symbol)
        try:
            r = requests.get(
                f'https://finance.sina.com.cn/realstock/company/{code}/nc.shtml',
                headers=cls._HEADERS, timeout=Config.REQUEST_TIMEOUT
            )
            if r.status_code != 200:
                raise RuntimeError(f"新浪请求失败 status={r.status_code}")

            import re
            data_match = re.search(r'var hq_str_(\w+)="([^"]*)"', r.text)
            if not data_match:
                raise RuntimeError(f"新浪未匹配到 {symbol} 的行情数据")

            parts = data_match.group(2).split(',')
            if len(parts) < 40:
                raise RuntimeError(f"新浪行情数据不完整 {symbol}")

            result = {
                '股票代码': symbol,
                '名称': parts[0] if len(parts) > 0 else '',
                '最新价': _safe_float(parts[3]) if len(parts) > 3 else 0,
            }

            try:
                detail_r = requests.get(
                    f'https://vip.stock.finance.sina.com.cn/corp/go.php/vFD_FinancialGuideLine/stockid/{symbol}/ctrl/2024/displaytype/4.phtml',
                    headers=cls._HEADERS, timeout=Config.REQUEST_TIMEOUT
                )
                if detail_r.status_code == 200:
                    pe_match = re.search(r'市盈率.*?<td[^>]*>([\d.]+)', detail_r.text)
                    if pe_match:
                        result['市盈率-动态'] = _safe_float(pe_match.group(1))
                    pb_match = re.search(r'市净率.*?<td[^>]*>([\d.]+)', detail_r.text)
                    if pb_match:
                        result['市净率'] = _safe_float(pb_match.group(1))
            except Exception:
                pass

            return pd.DataFrame([result])
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"新浪获取估值指标失败: {e}")

    @classmethod
    def get_valuation_history(cls, symbol: str, years: int = 5) -> pd.DataFrame:
        """新浪暂不支持历史估值数据"""
        raise NotImplementedError("新浪财经 不支持历史估值数据")

    @classmethod
    def get_industry_valuation(cls, industry_name: str) -> pd.DataFrame:
        """通过新浪财经获取行业估值统计（基于板块成分股行情聚合）"""
        try:
            df_cons = cls.get_board_industry_cons(industry_name)
            if df_cons is None or df_cons.empty:
                raise RuntimeError(f"新浪未获取到行业 {industry_name} 的成分股")

            code_col = '代码' if '代码' in df_cons.columns else None
            if code_col is None:
                raise RuntimeError(f"新浪行业成分股缺少代码列")

            codes = df_cons[code_col].tolist()[:50]

            all_stocks = []
            for code in codes:
                try:
                    sina_code = cls._sina_code(code)
                    r = requests.get(
                        f'https://hq.sinajs.cn/list={sina_code}',
                        headers={**cls._HEADERS, 'Referer': 'https://finance.sina.com.cn'},
                        timeout=5
                    )
                    if r.status_code == 200:
                        import re
                        match = re.search(r'="([^"]*)"', r.text)
                        if match:
                            parts = match.group(1).split(',')
                            if len(parts) >= 40:
                                all_stocks.append({
                                    '代码': code,
                                    '名称': parts[0],
                                    '最新价': _safe_float(parts[3]),
                                })
                    time.sleep(0.1)
                except Exception:
                    continue

            if not all_stocks:
                raise RuntimeError(f"新浪未获取到行业 {industry_name} 的行情数据")

            spot_df = cls.get_spot_em()
            if spot_df.empty:
                raise RuntimeError(f"新浪未获取到实时行情数据")

            industry_stocks = spot_df[spot_df['代码'].isin(codes)]
            if industry_stocks.empty:
                raise RuntimeError(f"新浪行业 {industry_name} 无匹配行情")

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
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"新浪获取行业估值失败: {e}")

    # ── 资金流向 / 融资融券 / 大宗交易 / 板块轮动 ──
    # 新浪对这些数据无直接 API，统一抛出 NotImplementedError

    @classmethod
    def get_individual_fund_flow(cls, symbol: str) -> pd.DataFrame:
        raise NotImplementedError("新浪不支持资金流向")

    @classmethod
    def get_sector_fund_flow_rank(cls, indicator: str = "今日", sector_type: str = "行业资金流") -> pd.DataFrame:
        raise NotImplementedError("新浪不支持板块资金流向排名")

    @classmethod
    def get_north_fund_flow(cls, symbol: str = "北向资金") -> pd.DataFrame:
        raise NotImplementedError("新浪不支持北向资金")

    @classmethod
    def get_margin_trading(cls, market: str = "sh", start_date: str = "", end_date: str = "") -> pd.DataFrame:
        raise NotImplementedError("新浪不支持融资融券")

    @classmethod
    def get_margin_detail(cls, market: str = "sh", date: str = "") -> pd.DataFrame:
        raise NotImplementedError("新浪不支持融资融券明细")

    @classmethod
    def get_block_trades(cls, symbol: str = "A股", start_date: str = "", end_date: str = "") -> pd.DataFrame:
        raise NotImplementedError("新浪不支持大宗交易")

    @classmethod
    def get_block_trade_stats(cls, start_date: str = "", end_date: str = "") -> pd.DataFrame:
        raise NotImplementedError("新浪不支持大宗交易统计")

    @classmethod
    def get_board_industry_spot(cls, symbol: str = "行业板块") -> pd.DataFrame:
        raise NotImplementedError("新浪板块实时行情暂不支持")

    @classmethod
    def get_board_industry_hist(cls, symbol: str, period: str = "daily", start: str = "", end: str = "") -> pd.DataFrame:
        raise NotImplementedError("新浪板块历史行情暂不支持")