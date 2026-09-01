"""LLM 网关策略层：ratelimit / router / retry / breaker。

每个策略是独立可测单元，网关按固定顺序编排它们（详见 gateway.py）。
"""
