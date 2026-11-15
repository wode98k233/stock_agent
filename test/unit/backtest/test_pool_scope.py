"""标的池范围解析：对话里的「范围说法」→ 可回测代码清单。"""
import pytest

import backtest.pool_scope as ps
import backtest.symbol_alias as SA

# 别名/指数名在库里（数据源 data/symbol_aliases.json）：用临时库，不读本机真库
pytestmark = pytest.mark.usefixtures("symbol_alias_db")


# ── 原话 → spec（确定性，不调 LLM）────────────────────────────

def test_parse_scope_text_recognizes_sources():
    assert ps.parse_scope_text('沪深300成分股里验证') == 'POOL:index:000300'
    assert ps.parse_scope_text('中证500 里跑') == 'POOL:index:000905'
    assert ps.parse_scope_text('我的自选股里跑50只看看') == 'POOL:watchlist|limit=50'
    assert ps.parse_scope_text('我的持仓里验证') == 'POOL:portfolio'
    assert ps.parse_scope_text('自选股和我的持仓都跑') == 'POOL:union'
    assert ps.parse_scope_text('医药板块') == 'POOL:board:医药'
    assert ps.parse_scope_text('600519 和 510300 都跑') == 'POOL:codes:600519,510300'
    assert ps.parse_scope_text('只看ETF，默认池先跑20只') == 'POOL:default|limit=20|kind=etf'


# ── P1：别名库缺失/空表时的行为（db/ 不进 git，新环境必踩）──────────

def test_missing_alias_db_auto_seeds_and_keeps_index_scope(tmp_path, monkeypatch):
    """库文件根本不存在 → 自动补种 → 仍然是「指数范围」，绝不落成 POOL:codes:000300。

    复现根因：`db/` 不进 git，新环境没跑 seed 就查不到 `沪深300 → 000300`，
    旧实现退化到 `stock_info` 名字查（本机 stock_info 里也有 000300），
    于是把指数当个股回测（`_auto_detect_asset_type('000300') == 'stock'`）。
    """
    db = tmp_path / 'fresh' / 'market_data.db'
    monkeypatch.setattr(SA, '_db_path', lambda: str(db))
    SA._READY.clear()

    assert ps.parse_scope_text('跑沪深300') == 'POOL:index:000300'
    assert ps.parse_scope_text('沪深300成分股里验证') == 'POOL:index:000300'
    assert SA.is_ready() is True and db.exists()


def test_unusable_alias_data_never_swaps_index_for_stock(tmp_path, monkeypatch):
    """连数据文件都没有（补种也失败）→ 不许猜成代码池；宁可「没解析出范围」。

    `_lookup_name` 桩成真机上会发生的那种命中：stock_info 里有 000300/沪深300 的脏行。
    守卫靠 index_info 的**名字一致**判定它是指数（`000001` 平安银行不会被误杀）。
    """
    monkeypatch.setattr(SA, '_db_path', lambda: str(tmp_path / 'nope' / 'market_data.db'))
    monkeypatch.setattr(SA, 'SEED_PATH', tmp_path / 'missing_seed.json')
    SA._READY.clear()
    monkeypatch.setattr('utils.cache.market_data_db.get_index_name',
                        lambda code: '沪深300' if str(code) == '000300' else None)
    monkeypatch.setattr(ps, '_lookup_name',
                        lambda n: ('000300', '沪深300') if n == '沪深300' else ('', ''))

    assert ps.parse_scope_text('跑沪深300') is None
    detail = ps.parse_scope_details('跑沪深300')
    assert detail['spec'] is None and detail['codes'] == []
    assert ps.parse_scope_text('跑沪深300') != 'POOL:codes:000300'      # 明确的验收断言


def test_codes_spec_rejects_index_code(monkeypatch):
    """`POOL:codes:000300` = 把指数当个股回测 → 入口必须拒绝并给出改法。"""
    monkeypatch.setattr('utils.cache.market_data_db.get_index_name',
                        lambda code: '沪深300' if str(code) == '000300' else None)
    monkeypatch.setattr('utils.cache.market_data_db.get_stock_info',
                        lambda code: {'name': '沪深300'} if str(code) == '000300' else None)
    monkeypatch.setattr('utils.cache.market_data_db.resolve_symbol_market',
                        lambda code, name_hint='': 'INDEX' if str(code) == '000300' else 'STOCK')

    with pytest.raises(ValueError) as e:
        ps.resolve_scope('POOL:codes:000300')
    assert 'POOL:index:000300' in str(e.value)
    with pytest.raises(ValueError):
        ps.build_spec('codes', '000300')
    with pytest.raises(ValueError):
        ps.build_spec('codes', '600519,000300')      # 混在列表里也要拒

    # 正常代码/ETF 不受影响；000001 是「上证指数 + 平安银行」重叠码，个股侧不动
    assert ps.build_spec('codes', '600519,510300') == 'POOL:codes:600519,510300'
    assert ps.build_spec('codes', '000001') == 'POOL:codes:000001'
    # 解析器遇到用户手写的指数码也不落成代码池
    assert ps.parse_scope_text('000300 和 600519 都跑') is None


def test_etf_name_containing_index_alias_is_not_index_scope():
    """「沪深300ETF」含子串「沪深300」，但点名的是一只 ETF，不能静默变成成分股范围。

    真机 2026-10-25 的「静默换标的」就是这类：用户点名 510300，跑成了沪深300 成分股。
    """
    assert ps.parse_scope_text('只做沪深300ETF这一个') == 'POOL:codes:510300'
    assert ps.parse_scope_text('跑沪深300ETF') == 'POOL:codes:510300'
    # 真指数说法不受影响
    assert ps.parse_scope_text('沪深300 里跑') == 'POOL:index:000300'


def test_parse_scope_text_mixed_stocks_and_etf():
    assert ps.parse_scope_text('个股ETF也是要混合的') == 'POOL:mixed'
    assert ps.parse_scope_text('个股ETF混合直到找到100只标的') == 'POOL:mixed|limit=100'
    assert ps.parse_scope_text('扩大点范围，但是也别这么大，个股和ETF混合总数达到100就行') \
        == 'POOL:mixed|limit=100'
    assert ps.parse_scope_text('混合池') == 'POOL:mixed'


def test_resolve_mixed_scope(monkeypatch):
    rows = [{'code': f'6000{i:02d}', 'as_of_date': '2026-10-16'} for i in range(10)]
    monkeypatch.setattr('backtest.factor_data.load_universe', lambda code: rows)
    out = ps.resolve_scope('POOL:mixed')
    assert '510300' in out['codes']          # DEFAULT_ETFS 里的沪深300ETF
    assert len(out['codes']) == 25           # 10 股票 + 15 ETF
    assert '沪深300 + ETF 混合' in out['label']
    assert out['short'] == '混合池'


def test_parse_scope_text_does_not_misfire_on_strategy_words():
    """「持有3个交易日」「3只股票」不是范围，不能被误判成池子。"""
    for text in ('市场深V反弹，第二天开盘买入，持有3个交易日卖出',
                 '持有3只股票各买一万',
                 '600519 单只跑'):
        assert ps.parse_scope_text(text) is None, text


USER_IDEA = (
    '深V反弹策略回测一下，标的用和科技相关的个股和ETF吧，比如CPO（通信ETF），'
    '半导体ETF，科创50ETF，人工智能ETF，剑桥科技，易中天这三个光模块的龙头，等等')


def test_named_targets_resolve_to_codes_not_mixed(monkeypatch):
    """点名的中文股名 / ETF 简称 / 行业俗称必须落成代码池，不能静默换成混合池。

    真机 2026-10-25：用户点名了上面这些标的，旧实现认不出名字 → 静默回退
    POOL:mixed，跑成了「沪深300 + ETF 315 只」，与用户要的科技标的完全无关。
    """
    monkeypatch.setattr(ps, '_lookup_name',
                        lambda n: {'剑桥科技': ('603083', '剑桥科技')}.get(n, ('', '')))
    spec = ps.parse_scope_text(USER_IDEA)
    assert spec == 'POOL:codes:515880,512480,588000,159819,603083,300502,300308,300394'
    assert 'mixed' not in spec and 'default' not in spec
    detail = ps.parse_scope_details(USER_IDEA)
    assert detail['unresolved'] == []
    assert detail['codes'][4:] == ['603083', '300502', '300308', '300394']  # 易中天三家


def test_codes_label_lists_names(monkeypatch):
    """代码池标签必须带名字：只写「指定代码」用户看不出自己点的票进没进池子。"""
    monkeypatch.setattr(ps, '_code_names',
                        lambda codes: {'603083': '剑桥科技', '515880': '通信ETF国泰'})
    out = ps.resolve_scope('POOL:codes:603083,515880')
    assert '剑桥科技(603083)' in out['label']
    assert '通信ETF国泰(515880)' in out['label']
    assert out['short'] == '指定 2 只'


def test_unresolved_names_are_reported(monkeypatch):
    """认不出的名字要如实带回（调用方会写进确认卡），不能悄悄消失。"""
    monkeypatch.setattr(ps, '_lookup_name',
                        lambda n: {'剑桥科技': ('603083', '剑桥科技')}.get(n, ('', '')))
    detail = ps.parse_scope_details('标的用剑桥科技和量子ETF和某某科技')
    assert '量子ETF' in detail['unresolved'] and '某某科技' in detail['unresolved']
    # 整句一只都没认出时，不把普通短语当标的（否则「收获如何」会被写进卡）
    only_unknown = ps.parse_scope_details('标的用某某科技')
    assert only_unknown['unresolved'] == []


def test_single_named_stock_becomes_code_scope(monkeypatch):
    """只点名一只（不是手写代码）也走代码池；手写单个代码仍保持单票语义。"""
    monkeypatch.setattr(ps, '_lookup_name',
                        lambda n: {'剑桥科技': ('603083', '剑桥科技')}.get(n, ('', '')))
    assert ps.parse_scope_text('只做剑桥科技这一个') == 'POOL:codes:603083'
    assert ps.parse_scope_text('600519 单只跑') is None
    assert ps.parse_scope_text('个股ETF混合，比如剑桥科技') == 'POOL:mixed'


def test_build_spec_and_is_scope_spec():
    assert ps.build_spec('index', '沪深300', limit=50) == 'POOL:index:000300|limit=50'
    assert ps.build_spec('codes', '600519,510300') == 'POOL:codes:600519,510300'
    assert ps.is_scope_spec('POOL:index:000300')
    assert ps.is_scope_spec('POOL:watchlist')
    assert ps.is_scope_spec('POOL:default|limit=20')
    assert not ps.is_scope_spec('POOL:default')
    assert not ps.is_scope_spec('POOL:600519,510300')
    with pytest.raises(ValueError):
        ps.build_spec('board', '')


# ── spec → 清单（DB 读取全部打桩，单测不碰真库）──────────────

def test_resolve_index_scope_with_limit(monkeypatch):
    rows = [{'code': f'6000{i:02d}', 'as_of_date': '2026-10-16'} for i in range(10)]
    monkeypatch.setattr('backtest.factor_data.load_universe', lambda code: rows)
    out = ps.resolve_scope('POOL:index:000300|limit=3')
    assert out['codes'] == ['600000', '600003', '600006']      # 均匀抽，可复现
    assert '沪深300 成分股' in out['label']
    assert '从 10 只里均匀抽 3 只' in out['label']
    assert '2026-10-16' in out['label']
    assert out['short'] == '沪深300'


def test_resolve_index_scope_empty_raises(monkeypatch):
    monkeypatch.setattr('backtest.factor_data.load_universe', lambda code: [])
    with pytest.raises(ValueError) as e:
        ps.resolve_scope('POOL:index:000300')
    assert '回补' in str(e.value)


def test_resolve_watchlist_portfolio_union(monkeypatch):
    monkeypatch.setattr(ps, '_watchlist_codes', lambda: ['600519', '510300'])
    monkeypatch.setattr(ps, '_portfolio_codes', lambda: ['600519', '000001'])
    w = ps.resolve_scope('POOL:watchlist')
    assert w['codes'] == ['600519', '510300'] and w['short'] == '自选'
    p = ps.resolve_scope('POOL:portfolio')
    assert p['codes'] == ['600519', '000001']
    u = ps.resolve_scope('POOL:union')
    assert u['codes'] == ['600519', '510300', '000001']        # 去重保序
    assert '自选 ∪ 持仓' in u['label']


def test_resolve_empty_scope_raises(monkeypatch):
    monkeypatch.setattr(ps, '_watchlist_codes', lambda: [])
    with pytest.raises(ValueError) as e:
        ps.resolve_scope('POOL:watchlist')
    assert '自选股是空的' in str(e.value)


def test_resolve_kind_filter(monkeypatch):
    monkeypatch.setattr(ps, '_watchlist_codes', lambda: ['600519', '510300', '159915'])
    out = ps.resolve_scope('POOL:watchlist|kind=etf')
    assert out['codes'] == ['510300', '159915']
    assert '仅 ETF' in out['label']
    stock = ps.resolve_scope('POOL:watchlist|kind=stock')
    assert stock['codes'] == ['600519']


def test_resolve_board_scope(monkeypatch):
    monkeypatch.setattr(ps, '_board_scope',
                        lambda name: (['600276', '000538'], '板块「医药」', '2026-09-06'))
    out = ps.resolve_scope('POOL:board:医药')
    assert out['codes'] == ['600276', '000538']
    assert out['label'].startswith('板块「医药」')
    assert '2026-09-06' in out['label']


def test_resolve_default_scope():
    out = ps.resolve_scope('POOL:default')
    assert len(out['codes']) == 40 and out['short'] == '默认池'


def test_describe_scope_never_raises(monkeypatch):
    def boom(spec):
        raise ValueError('没有快照')
    monkeypatch.setattr(ps, 'resolve_scope', boom)
    assert '无法解析' in ps.describe_scope('POOL:index:000300')
