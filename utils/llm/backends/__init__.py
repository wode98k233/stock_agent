"""LLM 网关 backend 层：chat / embed / rerank 的具体 provider 实现。

网关（gateway.py）只依赖这里的接口，不关心底层是 OpenAI / Ollama / 本地模型。
要加新 provider：在对应文件加一个分支即可，网关代码无需改动。
"""
