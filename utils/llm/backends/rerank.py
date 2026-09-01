"""
选股雷达 — rerank backend
=======================

把 ``RerankSpec`` 翻译成 ``RerankProvider``，对外只暴露 ``rerank(query, docs) -> list[float]``。

支持两种 provider（与 ``memory/backend/hybrid.py::Reranker`` 逻辑一致，抽出来统一接管）：
  - ``local``：本地 ``sentence_transformers.CrossEncoder``（零成本，推荐）
  - ``openai``：OpenAI 兼容 ``/v1/rerank``

关键点（沿用 hybrid.Reranker 的经验）：
- 本地 CrossEncoder（2GB+）**懒加载**，不在构造期加载，避免拖慢启动；
- 加载失败标记 ``available=False``，网关调用时静默降级（返回全 0 分）。
- 支持后台 ``warm()`` 预热，不阻塞启动流程。

扩展指引：
- 加新 provider（如 Cohere rerank）：在 ``rerank`` 里加分支，网关无需改动。
"""

import logging
import os
import threading
from typing import List, Optional

from utils.llm.types import RerankSpec

_log = logging.getLogger(__name__)


class RerankProvider:
    """Cross-encoder 精排器（可选）。"""

    def __init__(self, spec: RerankSpec):
        self.spec = spec
        self._model = None
        self._load_attempted = False
        self._load_lock = threading.Lock()
        if self.spec.provider == "local":
            self._available = True
        elif self.spec.provider == "openai":
            self._available = bool(self.spec.api_key and self.spec.base_url)
        else:
            self._available = False

    def is_available(self) -> bool:
        return self._available

    def warm(self) -> None:
        """后台预热本地模型（daemon 线程，不阻塞调用方）。"""
        if self.spec.provider != "local" or self._model is not None or self._load_attempted:
            return
        threading.Thread(target=self._ensure_local_model, name="reranker-warm", daemon=True).start()

    def _ensure_local_model(self) -> bool:
        """首次使用时懒加载本地 CrossEncoder（线程安全）。"""
        if self._model is not None:
            return True
        with self._load_lock:
            if self._model is not None:
                return True
            if self._load_attempted:
                return False
            self._load_attempted = True
            try:
                from sentence_transformers import CrossEncoder

                if self.spec.local_dir and os.path.isdir(self.spec.local_dir):
                    self._model = CrossEncoder(self.spec.local_dir)
                else:
                    self._model = CrossEncoder(self.spec.model, local_files_only=True)
                return True
            except Exception:
                _log.warning("[rerank] 本地模型加载失败，降级为不精排: %s", self.spec.model, exc_info=True)
                self._available = False
                return False

    def rerank(self, query: str, docs: List[str]) -> List[float]:
        """对候选文档精排，返回与 docs 等长的相关性分数。不可用时返回全 0（不精排）。"""
        if not self._available or not docs:
            return [0.0] * len(docs)

        if self.spec.provider == "local":
            if not self._ensure_local_model():
                return [0.0] * len(docs)
            try:
                pairs = [[query, d[:1024]] for d in docs]
                scores = self._model.predict(pairs, show_progress_bar=False).tolist()
                return scores
            except Exception:
                _log.warning("[rerank] 本地 predict 失败，降级为不精排", exc_info=True)
                return [0.0] * len(docs)

        if self.spec.provider == "openai":
            # 注意：硅基流动等兼容平台的 rerank 是自定义端点 POST {base}/rerank，
            # OpenAI SDK 没有 client.rerank 方法（会 AttributeError 静默降级），必须直接 HTTP 调用。
            try:
                import httpx

                base = (self.spec.base_url or "").rstrip("/")
                if not base.endswith("/v1"):
                    base += "/v1"
                url = f"{base}/rerank"
                payload = {
                    "model": self.spec.model,
                    "query": query,
                    "documents": [d[:1024] for d in docs],
                }
                headers = {"Authorization": f"Bearer {self.spec.api_key}"}
                resp = httpx.post(url, json=payload, headers=headers, timeout=60.0)
                resp.raise_for_status()
                data = resp.json()
                score_map = {
                    r["index"]: r["relevance_score"]
                    for r in data.get("results", [])
                }
                return [score_map.get(i, 0.0) for i in range(len(docs))]
            except Exception:
                _log.warning("[rerank] openai rerank 失败，降级为不精排", exc_info=True)
                return [0.0] * len(docs)

        return [0.0] * len(docs)
