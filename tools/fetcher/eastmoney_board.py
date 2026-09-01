"""东财 push2 板块成分股直连（按 BK 代码）。

背景：akshare 的 stock_board_*_cons_em 偶发被东财掐连接（RemoteDisconnected），
且 akshare 1.18.51 没有同花顺成分股接口（THS 仅有板块列表/简介/指数，无成分股）。
此处用更轻量的 push2 clist 单请求，按已采集到的 BK 代码直接取成分股，作为板块成分
采集的首选路径——单次 JSON 请求比 akshare 的多步流程更不容易被掐。
"""
import logging

import pandas as pd
import requests

from .base import DataSource
from .config import Config

logger = logging.getLogger("radar.fetcher")


def fetch_board_cons_by_bk(bk_code: str) -> pd.DataFrame:
    """按东财板块 BK 代码（如 BK0477）获取成分股。

    Args:
        bk_code: 东财板块代码，形如 ``BK0477``。

    Returns:
        含 ``代码/名称/最新价/涨跌幅`` 列的 DataFrame；无数据返回空 DataFrame。
    """
    if not bk_code:
        return pd.DataFrame()

    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": 1, "pz": 1000, "po": 1, "np": 1,
        "fltt": 2, "invt": 2, "fid": "f3",
        "fs": f"b:{bk_code}",
        "fields": "f12,f14,f2,f3",
    }
    headers = DataSource._set_random_user_agent()  # fake_useragent 随机 UA（带静态兜底）

    resp = requests.get(url, params=params, headers=headers, timeout=Config.REQUEST_TIMEOUT)
    resp.raise_for_status()
    diff = (resp.json().get("data") or {}).get("diff") or []
    if not diff:
        return pd.DataFrame()

    rows = []
    for d in diff:
        raw_code = str(d.get("f12", ""))
        code = raw_code.zfill(6) if raw_code.isdigit() else raw_code
        rows.append({
            "代码": code,
            "名称": str(d.get("f14", "")),
            "最新价": d.get("f2"),
            "涨跌幅": d.get("f3"),
        })
    return pd.DataFrame(rows)
