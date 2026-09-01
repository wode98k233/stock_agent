"""
选股雷达 — embedding backend
==========================

把 ``EmbedSpec`` 翻译成一个 ``EmbeddingProvider``，对外只暴露 ``embed(texts) -> list[list[float]]``。

支持三种 provider（与 ``memory/sdk.py::_create_embedding_fn`` 逻辑一致，但抽出来统一接管）：
  - ``openai``  / ``remote``：OpenAI 兼容 ``/v1/embeddings``
  - ``ollama``：Ollama 原生 ``/api/embeddings``（用 ``prompt`` 字段，内网不走代理）
  - ``local``：本地 ``sentence_transformers.SentenceTransformer``

设计：provider 实例可复用（OpenAI client / 本地模型只加载一次）。网关负责在它外面套
限流 / 重试 / 熔断 / 计量。

扩展指引：
- 加新 provider（如 Jina / 智谱）：在 ``embed`` 里加一个分支，网关无需改动。
"""

from typing import List, Optional

from utils.llm.types import EmbedSpec


class EmbeddingProvider:
    """文本 → 向量。线程安全前提：同一 provider 实例在多线程下复用（client 线程安全）。"""

    def __init__(self, spec: EmbedSpec):
        self.spec = spec
        self._client = None          # OpenAI client（懒加载）
        self._local_model = None     # SentenceTransformer（懒加载）

    def embed(self, texts: List[str]) -> List[List[float]]:
        """批量 embedding，返回与 texts 等长的向量列表。"""
        if self.spec.provider in ("openai", "remote"):
            return self._embed_openai(texts)
        if self.spec.provider == "ollama":
            return self._embed_ollama(texts)
        return self._embed_local(texts)

    # ── openai / remote ──

    def _embed_openai(self, texts: List[str]) -> List[List[float]]:
        from openai import OpenAI
        import httpx

        if self._client is None:
            # 内网地址不走系统代理
            self._client = OpenAI(
                api_key=self.spec.api_key,
                base_url=self.spec.base_url,
                timeout=30,
                http_client=httpx.Client(proxy=None),
            )
        out: List[List[float]] = []
        for t in texts:
            resp = self._client.embeddings.create(input=t, model=self.spec.model)
            out.append(resp.data[0].embedding)
        return out

    # ── ollama ──

    def _embed_ollama(self, texts: List[str]) -> List[List[float]]:
        import requests

        base = self.spec.base_url or "http://localhost:11434"
        url = base.rstrip("/").removesuffix("/v1") + "/api/embeddings"
        out: List[List[float]] = []
        for t in texts:
            resp = requests.post(
                url,
                json={"model": self.spec.model, "prompt": t},
                timeout=30,
                proxies={"http": None, "https": None},  # 内网不走系统代理
            )
            resp.raise_for_status()
            out.append(resp.json()["embedding"])
        return out

    # ── local ──

    def _embed_local(self, texts: List[str]) -> List[List[float]]:
        if self._local_model is None:
            from sentence_transformers import SentenceTransformer

            self._local_model = SentenceTransformer(self.spec.local_model or "BAAI/bge-small-zh-v1.5")
        return [self._local_model.encode(t).tolist() for t in texts]
