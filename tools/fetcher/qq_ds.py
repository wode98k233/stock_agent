"""腾讯财经直连数据源（纯HTTP，独立于东财）"""
import logging
import os
import requests
import pandas as pd
from datetime import datetime, timedelta
from .base import DataSource
from .config import Config

logger = logging.getLogger("radar.fetcher")


class QQFinanceDataSource(DataSource):
    name: str = "qq_finance"
    label: str = "腾讯财经"
    description: str = "腾讯实时行情，仅 A 股"
    priority: int = int(os.getenv("QQ_PRIORITY", "50"))

    _HEADERS = {
        'Referer': 'https://stockapp.finance.qq.com',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }

    @classmethod
    def is_available(cls) -> bool:
        if not cls.enabled:
            return False
        try:
            r = requests.get("https://qt.gtimg.cn/q=sh000001",
                             headers=cls._HEADERS, timeout=5)
            return r.status_code == 200 and '~' in r.text
        except:
            return False

    @classmethod
    def _qq_code(cls, symbol):
        from .base import _standardize_stock_code
        return _standardize_stock_code(symbol)

    @classmethod
    def _parse_tencent_quote(cls, text):
        """解析腾讯行情字符串格式 v_code="字段1~字段2~...~字段n~" """
        results = []
        for line in text.strip().split('\n'):
            line = line.strip()
            if not line or '~' not in line:
                continue
            # 去掉 v_xxxxxxx=" 和末尾的 "
            eq_idx = line.find('=')
            if eq_idx < 0:
                continue
            content = line[eq_idx + 1:].strip().strip('"')
            parts = content.split('~')
            if len(parts) < 45:
                continue
            name = parts[1]
            if not name:
                continue
            try:
                results.append({
                    '代码': parts[2],
                    '名称': name,
                    '最新价': float(parts[3]) if parts[3] else 0,
                    '昨收': float(parts[4]) if parts[4] else 0,
                    '开盘': float(parts[5]) if parts[5] else 0,
                    '成交量': int(float(parts[6])) if parts[6] else 0,
                    '成交额': float(parts[37]) if parts[37] else 0,
                    '涨跌幅': float(parts[32]) if parts[32] else 0,
                    '涨跌额': float(parts[31]) if parts[31] else 0,
                    '最高': float(parts[33]) if parts[33] else 0,
                    '最低': float(parts[34]) if parts[34] else 0,
                })
            except (ValueError, IndexError):
                continue
        return results

    # ── K线 ──────────────────────────────────────────────

    @classmethod
    def get_stock_hist(cls, symbol, period="daily", start="", end=""):
        from .base import _fetch_tencent_kline
        code = cls._qq_code(symbol)
        return _fetch_tencent_kline(code, period, start, end, cls._HEADERS)

    # ── 实时行情 ─────────────────────────────────────────

    @classmethod
    def get_spot_em(cls):
        """腾讯实时行情 — 使用常用股票列表分批查询"""
        try:
            # 常用主板+创业板代码前缀（精简版，覆盖主要股票）
            code_list = []
            # 沪市主板
            for prefix in ['600', '601', '603', '605']:
                for i in range(0, 200):
                    code_list.append(cls._qq_code(f'{prefix}{str(i).zfill(3)}'))
            # 科创板
            for i in range(0, 200):
                code_list.append(cls._qq_code(f'688{str(i).zfill(3)}'))
            # 深市主板
            for prefix in ['000', '001']:
                for i in range(0, 200):
                    code_list.append(cls._qq_code(f'{prefix}{str(i).zfill(3)}'))
            # 中小板
            for prefix in ['002', '003']:
                for i in range(0, 200):
                    code_list.append(cls._qq_code(f'{prefix}{str(i).zfill(3)}'))
            # 创业板
            for prefix in ['300', '301']:
                for i in range(0, 200):
                    code_list.append(cls._qq_code(f'{prefix}{str(i).zfill(3)}'))

            all_stocks = []
            batch_size = 80
            for i in range(0, len(code_list), batch_size):
                batch = code_list[i:i + batch_size]
                code_str = ','.join(batch)
                try:
                    r = requests.get(f'https://qt.gtimg.cn/q={code_str}',
                                     headers=cls._HEADERS, timeout=15)
                    if r.ok and r.text.strip():
                        parsed = cls._parse_tencent_quote(r.text)
                        all_stocks.extend(parsed)
                except Exception:
                    continue

            if all_stocks:
                df = pd.DataFrame(all_stocks)
                df = df[df['名称'].str.len() > 0]
                return df.drop_duplicates(subset=['代码']).reset_index(drop=True)
        except Exception as e:
            logger.error(f"腾讯获取行情失败: {e}")
        return pd.DataFrame()

    # ── 个股实时快照（单只）─────────────────────────────

    @classmethod
    def get_stock_snapshot(cls, symbol):
        """获取单只股票实时快照，返回 dict"""
        code = cls._qq_code(symbol)
        try:
            r = requests.get(f'https://qt.gtimg.cn/q={code}',
                             headers=cls._HEADERS, timeout=10)
            if r.ok and r.text.strip():
                parsed = cls._parse_tencent_quote(r.text)
                if parsed:
                    return parsed[0]
        except Exception as e:
            logger.error(f"腾讯获取快照失败: {e}")
        return {}

    # ── 腾讯不支持的功能 ──────────────────────────────────

    @classmethod
    def get_board_industry_cons(cls, symbol):
        raise NotImplementedError("腾讯财经 不支持行业板块成分股")

    @classmethod
    def get_board_concept_cons(cls, symbol):
        raise NotImplementedError("腾讯财经 不支持概念板块成分股")

    @classmethod
    def get_board_industry_list(cls):
        raise NotImplementedError("腾讯财经 不支持行业板块列表")

    @classmethod
    def get_board_concept_list(cls):
        raise NotImplementedError("腾讯财经 不支持概念板块列表")

    @classmethod
    def get_stock_news(cls, symbol):
        raise NotImplementedError("腾讯财经 不支持新闻查询")

    @classmethod
    def get_stock_rating(cls, symbol):
        raise NotImplementedError("腾讯财经 不支持机构评级")

    @classmethod
    def get_financial_abstract(cls, symbol):
        raise NotImplementedError("腾讯财经 不支持财务摘要")
