"""定价目录配置服务 / API 单测（设计文档 §12）。

⚠️ 全部落在临时目录：价目文件与备份目录都被 monkeypatch 到 tmp_path，
绝不碰仓库里的 data/pricing/pricing_catalog.json（那是用户自己的价格偏好）。
"""
import json
import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config import Config
from server import pricing_service as svc
from server.routes.pricing import router
from utils import pricing as P


def _default_catalog():
    """仓库内置默认价目（reset 的源；只读，不写仓库）"""
    return json.loads(P.default_catalog_path().read_text(encoding='utf-8'))


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """价目 + 备份目录全部指到 tmp_path（不写仓库）"""
    target = tmp_path / 'pricing_catalog.json'
    monkeypatch.setattr(Config, 'COST_PRICING_CATALOG_PATH', str(target), raising=False)
    monkeypatch.setattr(svc, '_BACKUP_DIR', tmp_path / 'backups')
    P.invalidate()
    yield target
    P.invalidate()


def _client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# --------------------------------------------------------------------------
# 1. 校验
# --------------------------------------------------------------------------

def test_validate_accepts_default_catalog():
    res = svc.validate_catalog(_default_catalog())
    assert res['errors'] == [], res['errors']


def test_validate_rejects_bad_window_and_negative_price():
    cat = _default_catalog()
    cat['peak_schedule']['windows'] = [['25:00', '12:00'], ['14:00', '14:00']]
    cat['providers']['deepseek']['models']['deepseek-v4.1-flash']['off_peak']['in_miss'] = -1
    res = svc.validate_catalog(cat)
    joined = ' / '.join(res['errors'])
    assert 'HH:MM' in joined
    assert '开始早于结束' in joined
    assert '不能为负' in joined


def test_validate_rejects_duplicate_alias_within_provider():
    """同一供应商内，一个写法只能对应一个模型（否则取哪套价不确定）"""
    cat = _default_catalog()
    models = cat['providers']['deepseek']['models']
    models['another-model'] = {'aliases': ['deepseek-flash'], 'off_peak': {'in_miss': 1, 'in_hit': 0, 'out': 1}}
    res = svc.validate_catalog(cat)
    assert any('别名' in e and '同时属于' in e for e in res['errors']), res['errors']


def test_validate_allows_same_model_across_providers():
    """同一个模型名可以由官方与中转同时提供：用哪套价由 base_url 的 host 决定"""
    res = svc.validate_catalog(_default_catalog())
    assert not [e for e in res['errors'] if '同时属于' in e]


# ── 按次计费服务（services 段，2026-10-18 加）─────────────────
# 它和 providers/models 是两套口径（元/次 vs 元/百万 token），校验也必须分开。

def test_validate_accepts_default_services_section():
    cat = _default_catalog()
    assert 'deepseek-search' in cat['services']
    assert svc.validate_catalog(cat)['errors'] == []


def test_validate_rejects_bad_service_unit_and_price():
    cat = _default_catalog()
    cat['services']['bad-unit'] = {'billing': 'paid', 'unit': 'per_token', 'price': 1}
    cat['services']['bad-price'] = {'billing': 'paid', 'unit': 'per_request', 'price': -1}
    cat['services']['bad-num'] = {'billing': 'paid', 'unit': 'per_request', 'price': 'abc'}
    joined = ' / '.join(svc.validate_catalog(cat)['errors'])
    assert '只支持 per_request' in joined
    assert '不能为负' in joined
    assert '必须是数字' in joined


def test_validate_rejects_bad_service_billing_and_shape():
    cat = _default_catalog()
    cat['services']['weird'] = {'billing': 'cheap', 'unit': 'per_request', 'price': 1}
    joined = ' / '.join(svc.validate_catalog(cat)['errors'])
    assert 'paid / free / unknown' in joined

    cat2 = _default_catalog()
    cat2['services'] = ['not-a-dict']
    assert any('services 必须是对象' in e for e in svc.validate_catalog(cat2)['errors'])


def test_validate_warns_on_zero_price_paid_service():
    cat = _default_catalog()
    cat['services']['deepseek-search']['price'] = 0
    res = svc.validate_catalog(cat)
    assert res['errors'] == []
    assert any('单价为 0' in w for w in res['warnings'])


def test_services_section_survives_save_and_roundtrip(sandbox):
    """⚠️ 前端整份回传 catalog：保存必须**原样保留** services 段，否则按次费用全变「未计价」。"""
    cat = _default_catalog()
    svc.save_catalog(cat)
    saved = json.loads(sandbox.read_text(encoding='utf-8'))
    assert saved['services']['deepseek-search']['price'] == 0.005
    assert saved['services']['deepseek-fetch']['unit'] == 'per_request'
    # 版本号照旧自增（保存语义不变）
    assert saved['version'] != cat['version']


def test_validate_warns_on_odd_peak_ratio_and_free_without_expiry():
    cat = _default_catalog()
    cat['providers']['deepseek']['models']['deepseek-v4.1-flash']['peak']['out'] = 9.0   # 应为 8.0
    cat['providers']['local-relay'].pop('valid_until', None)
    res = svc.validate_catalog(cat)
    assert res['errors'] == []
    assert any('不等于谷价' in w for w in res['warnings']), res['warnings']
    assert any('没有填有效期' in w for w in res['warnings']), res['warnings']


def test_validate_rejects_bad_valid_until():
    cat = _default_catalog()
    cat['providers']['local-relay']['valid_until'] = '2026/12/31'
    res = svc.validate_catalog(cat)
    assert any('valid_until' in e for e in res['errors'])


# --------------------------------------------------------------------------
# 2. 保存 / 备份 / 版本 / 热生效
# --------------------------------------------------------------------------

def test_save_writes_file_with_backup_and_bumps_version(sandbox):
    first = svc.save_catalog(_default_catalog())
    assert sandbox.exists()
    assert first['version'] == svc.datetime.now().strftime('%Y-%m-%d')
    # 第二次保存：版本自增，且备份了上一版
    cat = json.loads(sandbox.read_text(encoding='utf-8'))
    cat['providers']['deepseek']['models']['deepseek-v4.1-flash']['off_peak']['in_miss'] = 3.0
    second = svc.save_catalog(cat)
    assert second['version'] != first['version']
    assert Path(second['backup']).exists()
    backup = json.loads(Path(second['backup']).read_text(encoding='utf-8'))
    assert backup['providers']['deepseek']['models']['deepseek-v4.1-flash']['off_peak']['in_miss'] == 1.0


def test_save_is_atomic_and_hot_reloads(sandbox):
    """保存后不重启即按新价算（热失效缓存）"""
    svc.save_catalog(_default_catalog())
    q = P.resolve_quote(model='deepseek-flash', base_url='https://api.deepseek.com/v1')
    assert q.price_of('off_peak')['in_miss'] == 1.0
    cat = json.loads(sandbox.read_text(encoding='utf-8'))
    cat['providers']['deepseek']['models']['deepseek-v4.1-flash']['off_peak']['in_miss'] = 7.0
    svc.save_catalog(cat)
    q2 = P.resolve_quote(model='deepseek-flash', base_url='https://api.deepseek.com/v1')
    assert q2.price_of('off_peak')['in_miss'] == 7.0
    assert not list(sandbox.parent.glob('*.tmp')), '临时文件必须被原子替换掉'


def test_save_rejects_invalid_without_touching_file(sandbox):
    svc.save_catalog(_default_catalog())
    before = sandbox.read_bytes()
    mtime = sandbox.stat().st_mtime
    cat = json.loads(sandbox.read_text(encoding='utf-8'))
    cat['peak_schedule']['windows'] = [['09:00', '09:00']]
    with pytest.raises(ValueError):
        svc.save_catalog(cat)
    assert sandbox.read_bytes() == before
    assert sandbox.stat().st_mtime == mtime


def test_reset_restores_default(sandbox):
    cat = _default_catalog()
    cat['providers']['deepseek']['models']['deepseek-v4.1-flash']['off_peak']['in_miss'] = 42.0
    svc.save_catalog(cat)
    svc.reset_catalog()
    after = json.loads(sandbox.read_text(encoding='utf-8'))
    assert after['providers']['deepseek']['models']['deepseek-v4.1-flash']['off_peak']['in_miss'] == 1.0


def test_read_catalog_reports_runtime_state(sandbox):
    body = svc.read_catalog()
    assert body['overridden'] is False
    assert body['is_default'] is True
    assert body['path'] == str(sandbox)
    svc.save_catalog(_default_catalog())
    body2 = svc.read_catalog()
    assert body2['overridden'] is True and body2['is_default'] is False
    assert body2['mtime'] > 0


# --------------------------------------------------------------------------
# 3. API
# --------------------------------------------------------------------------

def test_api_get_and_put(sandbox):
    client = _client()
    got = client.get('/api/pricing/catalog')
    assert got.status_code == 200
    assert 'providers' in got.json()['catalog']

    cat = got.json()['catalog']
    cat['peak_schedule']['windows'] = [['09:00', '12:00']]
    res = client.put('/api/pricing/catalog', json={'catalog': cat})
    assert res.status_code == 200
    assert res.json()['success'] is True


def test_api_put_returns_400_with_reason(sandbox):
    client = _client()
    cat = client.get('/api/pricing/catalog').json()['catalog']
    cat['providers'] = {}
    res = client.put('/api/pricing/catalog', json={'catalog': cat})
    assert res.status_code == 400
    assert 'providers' in res.json()['detail']


def test_api_validate_and_trial(sandbox):
    client = _client()
    cat = client.get('/api/pricing/catalog').json()['catalog']
    v = client.post('/api/pricing/validate', json={'catalog': cat}).json()
    assert v['errors'] == []
    t = client.post('/api/pricing/trial', json={
        'model': 'deepseek-flash', 'base_url': 'https://api.deepseek.com/v1',
        'input_tokens': 1000000, 'cached_tokens': 200000, 'output_tokens': 100000,
    }).json()
    assert t['off_peak']['total'] == pytest.approx(1.204)
    assert t['peak']['total'] == pytest.approx(2.408)
    assert t['now']['tier'] in ('peak', 'off_peak')


def test_api_reset(sandbox):
    client = _client()
    cat = client.get('/api/pricing/catalog').json()['catalog']
    cat['providers']['deepseek']['models']['deepseek-v4.1-flash']['off_peak']['in_miss'] = 5.0
    client.put('/api/pricing/catalog', json={'catalog': cat})
    assert client.post('/api/pricing/reset').status_code == 200
    after = client.get('/api/pricing/catalog').json()['catalog']
    assert after['providers']['deepseek']['models']['deepseek-v4.1-flash']['off_peak']['in_miss'] == 1.0
