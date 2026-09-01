"""PDOR 模式 — Plan-Do-Observe-Reflex"""


def build_pdor_graph(checkpointer=None, **kwargs):
    """延迟导入，避免循环依赖"""
    from agents.pdor.graph import _build_pdor_graph
    return _build_pdor_graph(checkpointer=checkpointer, **kwargs)
