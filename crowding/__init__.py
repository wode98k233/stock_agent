"""全市场拥挤度扫描：标的池 → 分项读数 → 拥挤分 → 快照。

分层与 `thesis/` 保持一致：
- `pool.py`    —— 标的池与阈值的**定义**（`data/crowding/pool.json`，可手改、进版本管理）
- `engine.py`  —— **计算**（读本地日线、按需轮转补数、算分）
- `storage.py` —— **快照**（`db/crowding.db`，同一天只留一个点）
"""
from .pool import DEFAULT_POOL, load_pool, validate_pool
from .engine import CrowdingEngine
from .storage import CrowdingStorage

__all__ = ["DEFAULT_POOL", "load_pool", "validate_pool", "CrowdingEngine", "CrowdingStorage"]
