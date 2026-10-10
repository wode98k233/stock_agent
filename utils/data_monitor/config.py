"""数据监控配置读写（data_monitor_config.json）"""
import json
import os

DEFAULTS = {
    'extra_scan_dirs': [],       # 追加扫描目录（绝对路径）
    'extra_files': [],           # 追加单文件
    'exclude_patterns': [],      # 排除 glob，如 ['*.tmp']
    'snapshot_retention_days': 90,
    'log_min_lines': 200,
    'stale_cache_days': 30,
}


def _config_path() -> str:
    from utils.app_paths import get_data_monitor_config_path
    return get_data_monitor_config_path()


def load_config() -> dict:
    """读取配置；文件缺失/损坏时回退默认值。"""
    cfg = dict(DEFAULTS)
    path = _config_path()
    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def save_config(cfg: dict) -> None:
    data = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
    path = _config_path()
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def ensure_config_file() -> dict:
    """首次运行生成配置。返回当前配置。"""
    path = _config_path()
    if not os.path.isfile(path):
        save_config(DEFAULTS)
    return load_config()