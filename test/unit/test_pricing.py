"""成本计价内核单测（设计文档 docs/superpowers/specs/2026-10-13-report-cost-estimation-design.md §12）。

锁死四类最容易静默出错的契约：

1. **峰谷按北京时间判**：配成 UTC 会把高峰窗口整体挪 8 小时（09:00–12:00 → 01:00–04:00），
   成本直接差一倍且没人看得出来；
2. **别名归一**：`deepseek-flash` / `deepseek-v4-flash` / `deepseek-v4-pro` 都必须落到
   同一价目（模型改名不该改代码）；
3. **缓存拆分**：命中 token 按命中价、其余按未命中价，且命中数封顶输入；
4. **免费 ≠ 0 元**：免费源显示「免费」、未知源记 `null`（记 0 会被误读成「已计价且为零」）。
"""
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import Config
from utils import pricing as P

TZ = ZoneInfo('Asia/Shanghai')
DEFAULT_CATALOG = PROJECT_ROOT / P.DEFAULT_CATALOG_REL


def _ts(y, m, d, hh, mm=0):
    """北京时间某刻的 unix 时间戳"""
    return datetime(y, m, d, hh, mm, tzinfo=TZ).timestamp()


def _default_catalog():
    return json.loads(DEFAULT_CATALOG.read_text(encoding='utf-8'))


@pytest.fixture(autouse=True)
def _clean_state():
    P.invalidate()
    P._warned.clear()
    P._DOWNGRADED.clear()
    yield
    P.invalidate()
    P._warned.clear()
    P._DOWNGRADED.clear()


@pytest.fixture()
def catalog_file(tmp_path, monkeypatch):
    """把运行时价目指到临时文件（内容默认 = 仓库默认价目）"""
    path = tmp_path / 'pricing_catalog.json'
    path.write_text(json.dumps(_default_catalog(), ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(Config, 'COST_PRICING_CATALOG_PATH', str(path), raising=False)
    P.invalidate()
    return path


# --------------------------------------------------------------------------
# 1. 峰谷判定（北京时间）
# --------------------------------------------------------------------------

def test_peak_windows_use_beijing_time(catalog_file):
    # 2026-10-14 是周三
    assert P.is_peak(_ts(2026, 10, 14, 10, 0)) is True          # 上午高峰
    assert P.is_peak(_ts(2026, 10, 14, 14, 30)) is True         # 下午高峰
    assert P.is_peak(_ts(2026, 10, 14, 12, 30)) is False        # 午休
    assert P.is_peak(_ts(2026, 10, 14, 8, 59)) is False
    assert P.is_peak(_ts(2026, 10, 14, 20, 0)) is False         # 夜间
    # 半开区间 [start, end)：起点算高峰、终点不算
    assert P.is_peak(_ts(2026, 10, 14, 9, 0)) is True
    assert P.is_peak(_ts(2026, 10, 14, 12, 0)) is False
    assert P.is_peak(_ts(2026, 10, 14, 18, 0)) is False


def test_weekend_is_always_off_peak(catalog_file):
    assert P.is_peak(_ts(2026, 10, 17, 10, 0)) is False         # 周六
    assert P.is_peak(_ts(2026, 10, 18, 15, 0)) is False         # 周日


def test_peak_would_be_wrong_if_utc_used(catalog_file):
    """北京 10:00 = UTC 02:00：若误用 UTC，这一刻会被判成闲时（成本差一倍）"""
    ts = _ts(2026, 10, 14, 10, 0)
    utc_hour = datetime.fromtimestamp(ts, ZoneInfo('UTC')).hour
    assert utc_hour == 2
    assert P.is_peak(ts) is True, '必须按北京时间判峰谷'


# --------------------------------------------------------------------------
# 2. 模型别名归一 / 供应商解析
# --------------------------------------------------------------------------

def test_model_aliases_hit_same_price(catalog_file):
    base = P.resolve_quote(model='deepseek-v4.1-flash', base_url='https://api.deepseek.com/v1')
    assert base.billing == 'paid'
    for alias in ('deepseek-flash', 'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp',
                  'deepseek-v4-pro', 'DeepSeek v4.1 Flash', 'deepseek-v4.1-flash'):
        q = P.resolve_quote(model=alias, base_url='https://api.deepseek.com/v1')
        assert q.billing == 'paid', alias
        assert q.model == 'deepseek-v4.1-flash', alias
        assert q.price_of('off_peak') == base.price_of('off_peak'), alias


def test_provider_resolved_from_base_url_host(catalog_file):
    assert P.resolve_provider(base_url='https://api.deepseek.com/v1') == 'deepseek'
    assert P.resolve_provider(base_url='http://10.67.197.178:8000/v1') == 'local-relay'
    assert P.resolve_provider(base_url='https://nowhere.example.com/v1') == ''


def test_default_catalog_prices_match_official(catalog_file):
    """价目事实（元/百万 token）：闲时 1.0/0.02/4.0，高峰 2.0/0.04/8.0"""
    q = P.resolve_quote(model='deepseek-flash', base_url='https://api.deepseek.com/v1')
    assert q.price_of('off_peak') == {'in_miss': 1.0, 'in_hit': 0.02, 'out': 4.0}
    assert q.price_of('peak') == {'in_miss': 2.0, 'in_hit': 0.04, 'out': 8.0}


# --------------------------------------------------------------------------
# 3. 计价（手算对齐）
# --------------------------------------------------------------------------

USAGE = {'input_tokens': 1_000_000, 'cached_tokens': 200_000, 'output_tokens': 100_000}


def test_calc_cost_off_peak_manual(catalog_file):
    q = P.resolve_quote(model='deepseek-flash', base_url='https://api.deepseek.com/v1')
    cost = P.calc_cost(USAGE, q, _ts(2026, 10, 14, 22, 0))     # 夜间 = 闲时
    assert cost.tier == 'off_peak'
    assert cost.input_miss == pytest.approx(0.8)               # 80 万 × 1.0 / 1M
    assert cost.input_hit == pytest.approx(0.004)              # 20 万 × 0.02 / 1M
    assert cost.output == pytest.approx(0.4)                   # 10 万 × 4.0 / 1M
    assert cost.total == pytest.approx(1.204)
    assert cost.cached_saved == pytest.approx(0.196)           # 命中省下的钱


def test_calc_cost_peak_is_double(catalog_file):
    q = P.resolve_quote(model='deepseek-flash', base_url='https://api.deepseek.com/v1')
    cost = P.calc_cost(USAGE, q, _ts(2026, 10, 14, 10, 0))
    assert cost.tier == 'peak'
    assert cost.total == pytest.approx(2.408)
    assert cost.as_dict()['price_snapshot'] == {'in_miss': 2.0, 'in_hit': 0.04, 'out': 8.0}


def test_cached_tokens_capped_by_input(catalog_file):
    q = P.resolve_quote(model='deepseek-flash', base_url='https://api.deepseek.com/v1')
    cost = P.calc_cost({'input_tokens': 100, 'cached_tokens': 999, 'output_tokens': 0}, q, 0)
    assert cost.input_miss == 0.0
    assert cost.input_hit == pytest.approx(100 / 1_000_000 * 0.02)


def test_missing_cache_field_counts_as_miss(catalog_file):
    """供应商不返回缓存字段 → 全部按未命中（保守，宁高不低）"""
    q = P.resolve_quote(model='deepseek-flash', base_url='https://api.deepseek.com/v1')
    cost = P.calc_cost({'input_tokens': 1_000_000, 'output_tokens': 0}, q, _ts(2026, 10, 14, 22, 0))
    assert cost.input_hit == 0.0
    assert cost.input_miss == pytest.approx(1.0)


def test_reasoning_tokens_not_double_counted(catalog_file):
    """思考 token 已含在 output_tokens 内（utils/token_usage.py），不能再加一遍"""
    q = P.resolve_quote(model='deepseek-flash', base_url='https://api.deepseek.com/v1')
    cost = P.calc_cost({'input_tokens': 0, 'output_tokens': 1_000_000,
                        'reasoning_tokens': 800_000}, q, _ts(2026, 10, 14, 22, 0))
    assert cost.output == pytest.approx(4.0)


# --------------------------------------------------------------------------
# 4. 免费 / 未知 / 过期
# --------------------------------------------------------------------------

def test_free_provider_costs_zero_but_is_marked_free(catalog_file):
    q = P.resolve_quote(model='anything', base_url='http://10.67.197.178:8000/v1')
    assert q.billing == 'free'
    cost = P.calc_cost(USAGE, q, _ts(2026, 10, 14, 10, 0))
    assert cost.total == 0.0
    assert cost.billing == 'free'
    assert P.format_cost(cost) == '免费'


def test_unknown_provider_records_null(catalog_file):
    q = P.resolve_quote(model='gpt-4o', base_url='https://nowhere.example.com/v1')
    assert q.billing == 'unknown'
    cost = P.calc_cost(USAGE, q, _ts(2026, 10, 14, 10, 0))
    assert cost.total is None
    assert P.format_cost(cost) == '未计价'


def test_gift_endpoints_are_free_not_unpriced(catalog_file):
    """压缩模型（sensenova）与记忆 embedding（siliconflow）必须走「免费」而非「未计价」。

    回归起因：这两个 host 当时不在价目里 → resolve_provider 返回空 →
    total=None → 被聚合器排除在金额口径外，界面只显示主模型金额，
    看起来像「两个供应商一个价格」，实际是第二个供应商根本没计价。
    免费（total=0.0）与未计价（total=None）在这里必须区分开。
    """
    cases = [
        ('https://token.sensenova.cn/v1', 'deepseek-v4-flash', 'sensenova'),
        ('https://api.siliconflow.cn/v1/', 'BAAI/bge-m3', 'siliconflow'),
    ]
    for base_url, model, expect_provider in cases:
        q = P.resolve_quote(model=model, base_url=base_url)
        assert q.provider == expect_provider, f'{base_url} 应解析到 {expect_provider}，实际 {q.provider}'
        assert q.billing == 'free', f'{expect_provider} 应为免费源，实际 {q.billing}'
        cost = P.calc_cost(USAGE, q, _ts(2026, 10, 14, 10, 0))
        # 免费 = 已计价且金额为 0；未计价是 total=None，两者不可混
        assert cost.total == 0.0, f'{expect_provider} 免费源金额应为 0.0，实际 {cost.total}'
        assert P.format_cost(cost) == '免费'


def test_default_catalog_covers_all_configured_endpoints(catalog_file):
    """所有在用的模型端点都必须在价目里，否则运行时静默记未计价。

    兜住「.env 加了新 API_BASE 但没人同步价目」这类漂移：
    未计价不报错、只是数字悄悄少算，正是本次踩的坑。
    """
    hosts = set()
    for name in ('OPENAI_API_BASE', 'TOOL_COMPRESS_LLM_API_BASE', 'JUDGE_LLM_API_BASE',
                 'REPORT_LLM_API_BASE', 'STOCK_MEMORY_API_BASE',
                 'STOCK_MEMORY_RERANKER_API_BASE'):
        raw = str(getattr(Config, name, '') or '').strip()
        if raw:
            host = raw.split('://', 1)[-1].split('/', 1)[0].split(':', 1)[0]
            if host:
                hosts.add(host)
    assert hosts, '测试前提：应至少读到一组端点配置'
    unpriced = sorted(h for h in hosts if not P.resolve_provider(base_url=f'https://{h}/v1'))
    assert not unpriced, f'以下端点未收录进价目，成本会被记「未计价」：{unpriced}'


def test_free_source_past_valid_until_downgrades(catalog_file, caplog):
    cat = _default_catalog()
    cat['providers']['local-relay']['valid_until'] = '2020-01-01'
    catalog_file.write_text(json.dumps(cat, ensure_ascii=False), encoding='utf-8')
    P.invalidate()
    with caplog.at_level(logging.WARNING):
        q = P.resolve_quote(model='x', base_url='http://10.67.197.178:8000/v1')
        assert q.billing == 'unknown'
        # 同一个源只告警一次（免费到期是状态，不是每调用一条日志）
        P.resolve_quote(model='x', base_url='http://10.67.197.178:8000/v1')
    hits = [r for r in caplog.records if '免费源' in r.getMessage()]
    assert len(hits) == 1, [r.getMessage() for r in caplog.records]


def test_free_validity_check_can_be_disabled(catalog_file, monkeypatch):
    monkeypatch.setattr(Config, 'COST_FREE_VALIDITY_CHECK', False, raising=False)
    cat = _default_catalog()
    cat['providers']['local-relay']['valid_until'] = '2020-01-01'
    catalog_file.write_text(json.dumps(cat, ensure_ascii=False), encoding='utf-8')
    P.invalidate()
    assert P.resolve_quote(model='x', base_url='http://10.67.197.178:8000/v1').billing == 'free'


def test_unknown_model_warns_once(catalog_file, caplog):
    with caplog.at_level(logging.WARNING):
        assert P.resolve_quote(model='gpt-4o', base_url='https://api.deepseek.com/v1').billing == 'unknown'
        P.resolve_quote(model='gpt-4o', base_url='https://api.deepseek.com/v1')
    hits = [r for r in caplog.records if '不在供应商' in r.getMessage()]
    assert len(hits) == 1, [r.getMessage() for r in caplog.records]


# --------------------------------------------------------------------------
# 5. 入口与配置
# --------------------------------------------------------------------------

def test_compute_cost_respects_master_switch(catalog_file, monkeypatch):
    monkeypatch.setattr(Config, 'COST_TRACKING_ENABLED', False, raising=False)
    assert P.compute_cost(USAGE, model='deepseek-flash',
                          base_url='https://api.deepseek.com/v1', ts=0) is None


def test_compute_cost_unknown_policy_zero(catalog_file, monkeypatch):
    monkeypatch.setattr(Config, 'COST_UNKNOWN_PROVIDER_POLICY', 'zero', raising=False)
    cost = P.compute_cost(USAGE, model='gpt-4o', base_url='https://nowhere.example.com/v1', ts=0)
    assert cost is not None and cost.total == 0.0 and cost.billing == 'unknown'


def test_peak_multiplier_fallback(tmp_path, monkeypatch):
    """模型没显式给峰价时按「谷价 × peak_multiplier」兜底（页面上「峰=谷×2」一键填充同理）"""
    cat = _default_catalog()
    entry = cat['providers']['deepseek']['models']['deepseek-v4.1-flash']
    entry.pop('peak')
    path = tmp_path / 'c.json'
    path.write_text(json.dumps(cat, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(Config, 'COST_PRICING_CATALOG_PATH', str(path), raising=False)
    P.invalidate()
    q = P.resolve_quote(model='deepseek-flash', base_url='https://api.deepseek.com/v1')
    assert q.price_of('peak') == {'in_miss': 2.0, 'in_hit': 0.04, 'out': 8.0}


def test_missing_runtime_catalog_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, 'COST_PRICING_CATALOG_PATH',
                        str(tmp_path / 'not-there.json'), raising=False)
    P.invalidate()
    cat = P.load_catalog()
    assert cat.get('version') == _default_catalog().get('version')
    assert 'deepseek' in (cat.get('providers') or {})


def test_invalidate_picks_up_new_price(catalog_file):
    q = P.resolve_quote(model='deepseek-flash', base_url='https://api.deepseek.com/v1')
    assert q.price_of('off_peak')['in_miss'] == 1.0
    cat = _default_catalog()
    cat['providers']['deepseek']['models']['deepseek-v4.1-flash']['off_peak']['in_miss'] = 9.0
    catalog_file.write_text(json.dumps(cat, ensure_ascii=False), encoding='utf-8')
    P.invalidate()                                  # 保存后热生效（无需重启）
    q2 = P.resolve_quote(model='deepseek-flash', base_url='https://api.deepseek.com/v1')
    assert q2.price_of('off_peak')['in_miss'] == 9.0


def test_cost_enabled_defaults_true():
    assert P.cost_enabled() is True


def test_default_catalog_shape():
    cat = _default_catalog()
    assert cat['currency'] == 'CNY'
    assert cat['unit'] == 'per_1M_tokens'
    assert cat['peak_schedule']['tz'] == 'Asia/Shanghai'
    assert cat['peak_schedule']['windows'] == [['09:00', '12:00'], ['14:00', '18:00']]
    assert cat['peak_schedule']['weekdays'] == [1, 2, 3, 4, 5]


# --------------------------------------------------------------------------
# 11. 按次计费的服务（Search / Fetch，2026-10-18）
# --------------------------------------------------------------------------

def _with_services(cat, services):
    cat = json.loads(json.dumps(cat))
    cat['services'] = services
    return cat


def test_service_cost_multiplies_by_count(catalog_file):
    """按次计价 = 次数 × 单价；Search/Fetch 官方 ¥5/1000 次 = ¥0.005/次"""
    c = P.calc_service_cost('deepseek-search', 3)
    assert c.total == pytest.approx(0.015)
    assert c.count == 3 and c.unit == 'per_request' and c.billing == 'paid'
    assert c.as_dict()['kind'] == 'service'          # 落库必须自带 kind，聚合侧靠它分口径


def test_service_cost_has_no_peak_or_tier(catalog_file):
    """官方按次价不分峰谷：绝不能套 is_peak 造出「谷时折扣」的假象"""
    c = P.calc_service_cost('deepseek-fetch', 1)
    assert 'tier' not in c.as_dict()
    assert c.total == pytest.approx(0.005)


def test_service_cost_unknown_service_is_not_zero(catalog_file):
    """价目里没有这个服务 → total=None（未计价），**绝不填 0**"""
    c = P.calc_service_cost('no-such-service', 2)
    assert c.total is None and c.billing == 'unknown'
    assert P.format_service_cost(c) == '未计价'


def test_service_cost_free_entry_is_zero(catalog_file):
    cat = _with_services(_default_catalog(),
                         {'free-svc': {'billing': 'free', 'unit': 'per_request', 'price': 0.0}})
    catalog_file.write_text(json.dumps(cat, ensure_ascii=False), encoding='utf-8')
    P.invalidate()
    c = P.calc_service_cost('free-svc', 5)
    assert c.total == 0.0 and c.billing == 'free'      # 免费 ≠ 0 元：billing 必须留痕


def test_service_cost_snapshot_records_price_at_call_time(catalog_file):
    """记录时计价 + 价目快照：改价后旧账不能被改写"""
    before = P.calc_service_cost('deepseek-search', 1)
    assert before.price_snapshot == {'price': 0.005, 'unit': 'per_request'}

    cat = _with_services(_default_catalog(),
                         {'deepseek-search': {'billing': 'paid', 'unit': 'per_request', 'price': 0.02}})
    catalog_file.write_text(json.dumps(cat, ensure_ascii=False), encoding='utf-8')
    P.invalidate()
    assert P.calc_service_cost('deepseek-search', 1).total == pytest.approx(0.02)
    assert before.total == pytest.approx(0.005)        # 旧快照不受影响


def test_service_cost_switch_off_returns_none(monkeypatch, catalog_file):
    monkeypatch.setattr(Config, 'COST_TRACKING_ENABLED', False, raising=False)
    assert P.compute_service_cost('deepseek-search', 1) is None


def test_service_cost_zero_policy_fills_zero(monkeypatch, catalog_file):
    """COST_UNKNOWN_PROVIDER_POLICY=zero 时未计价按 0（与 LLM 侧同款开关）"""
    monkeypatch.setattr(Config, 'COST_UNKNOWN_PROVIDER_POLICY', 'zero', raising=False)
    c = P.compute_service_cost('no-such-service', 1)
    assert c is not None and c.total == 0.0


def test_service_count_never_negative(catalog_file):
    assert P.calc_service_cost('deepseek-search', -3).total == 0.0
