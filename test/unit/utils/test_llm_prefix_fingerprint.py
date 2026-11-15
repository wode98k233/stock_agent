"""Prompt 前缀指纹单测（PRD-A9 M-6）

provider 的 prompt cache 是「从头开始的连续前缀」字节匹配，渲染顺序
tools → system → messages。指纹只覆盖前两段（稳定前缀），用于事后跨请求 diff：
若两次请求指纹不同，说明前缀真的变了；指纹相同但没命中，才是 provider 侧的问题。
"""
import hashlib
import json

import pytest

from utils.llm_factory import TokenCompatibleChatOpenAI


def _mk(**over):
    base = dict(model="glm-4-flash", api_key="sk-test",
                base_url="https://example.invalid/v1")
    base.update(over)
    return TokenCompatibleChatOpenAI(**base)


class TestPrefixBytes:
    """_prefix_bytes：只取 tools + 连续 system 段，且渲染必须确定。"""

    def _tools(self, *names):
        return [{"type": "function", "function": {"name": n, "description": n}} for n in names]

    def test_tools_order_changes_fingerprint(self):
        a = _mk()._prefix_bytes({"tools": self._tools("t1", "t2")}, [])
        b = _mk()._prefix_bytes({"tools": self._tools("t2", "t1")}, [])
        assert a != b, "工具顺序变化必须改指纹（这正是每轮换工具会破缓存的检测点）"

    def test_dict_key_order_does_not_change_fingerprint(self):
        """同一份 tools 因 dict 序列化顺序不同，字节必须一致。"""
        t1 = {"type": "function", "function": {"name": "x", "description": "d", "parameters": {}}}
        t2 = {"type": "function", "function": {"description": "d", "parameters": {}, "name": "x"}}
        raw = json.dumps([t1], ensure_ascii=False, sort_keys=True)
        raw2 = json.dumps([t2], ensure_ascii=False, sort_keys=True)
        assert raw == raw2
        assert _mk()._prefix_bytes({"tools": [t1]}, []) == _mk()._prefix_bytes({"tools": [t2]}, [])

    def test_only_leading_system_messages_counted(self):
        msgs = [
            {"role": "system", "content": "A"},
            {"role": "system", "content": "B"},
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "回答"},
        ]
        tail = _mk()._prefix_bytes({}, msgs)
        assert tail == _mk()._prefix_bytes({}, msgs[:2]), "user 之后的消息不参与前缀"

    def test_growing_tail_does_not_change_fingerprint(self):
        """对话变长只应影响 messages 段；tools+system 不变则指纹不变。"""
        base = [{"role": "system", "content": "S"}]
        short = base + [{"role": "user", "content": "q1"}]
        long = base + [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a"},
                       {"role": "user", "content": "q2"}]
        assert _mk()._prefix_bytes({}, short) == _mk()._prefix_bytes({}, long)

    def test_empty_tools_and_system_gives_empty_bytes(self):
        raw = _mk()._prefix_bytes({}, [])
        assert isinstance(raw, bytes)
        assert raw == b""


class TestPrefixFingerprintAttach:
    """_get_request_payload：把指纹写进 payload.metadata 与 llm_output。"""

    def _payload_kwargs(self, llm, messages):
        return llm._get_request_payload(messages)

    def test_metadata_gets_prefix_fields(self):
        llm = _mk()
        payload = self._payload_kwargs(
            llm,
            [("system", "你是助手"), ("user", "你好")],
        )
        meta = payload.get("metadata")
        assert isinstance(meta, dict)
        fp = meta["prefix_sha256"]
        assert len(fp) == 16
        assert int(meta["prefix_bytes"]) > 0
        assert fp == hashlib.sha256(
            llm._prefix_bytes(payload, payload["messages"])
        ).hexdigest()[:16]

    def test_does_not_clobber_existing_metadata(self):
        llm = _mk()
        llm.model_kwargs = dict(getattr(llm, "model_kwargs", None) or {})
        llm.model_kwargs["metadata"] = {"user_track": "abc"}
        payload = self._payload_kwargs(llm, [("user", "hi")])
        assert payload["metadata"]["user_track"] == "abc"
        assert "prefix_sha256" in payload["metadata"]

    def test_same_prefix_same_fingerprint(self):
        llm = _mk()
        a = self._payload_kwargs(llm, [("system", "S"), ("user", "q")])
        b = self._payload_kwargs(llm, [("system", "S"), ("user", "q2")])
        assert a["metadata"]["prefix_sha256"] == b["metadata"]["prefix_sha256"]

    def test_different_system_gives_different_fingerprint(self):
        llm = _mk()
        a = self._payload_kwargs(llm, [("system", "S1"), ("user", "q")])
        b = self._payload_kwargs(llm, [("system", "S2"), ("user", "q")])
        assert a["metadata"]["prefix_sha256"] != b["metadata"]["prefix_sha256"]


def _mk_response_dict(model="deepseek-v4-flash"):
    return {
        "id": "1", "object": "chat.completion", "model": model,
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": "hi"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class TestBaseUrlBackfill:
    """_create_chat_result：把本次调用的**实际端点**回填到 llm_output.base_url。

    供应商/成本统计依赖它（2026-10-15 故障复盘）——同一模型可能经不同供应商
    （主端点 / 压缩旁路 / 报告旁路），只能按实际端点反查价目，不能用全局
    默认端点顶替，否则旁路调用会被误归到主端点供应商名下。
    """

    def test_llm_output_gets_base_url(self):
        llm = _mk(base_url="https://token.sensenova.cn/v1")
        result = llm._create_chat_result(_mk_response_dict())
        assert result.llm_output["base_url"] == "https://token.sensenova.cn/v1"

    def test_base_url_empty_when_unset(self, monkeypatch):
        """端点缺失时回填空串（提取端据此回落默认端点）。

        需隔离 env：测试环境 OPENAI_API_BASE 可能已从 .env 灌入，
        ChatOpenAI 构造会用它兜底 —— 那不是「缺失」形态。"""
        monkeypatch.delenv("OPENAI_API_BASE", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        llm = _mk(base_url=None)
        result = llm._create_chat_result(_mk_response_dict())
        assert result.llm_output["base_url"] == ""

    def test_coexists_with_prefix_fingerprint(self):
        """与 prefix_sha256 回填共存，互不覆盖"""
        llm = _mk(base_url="https://opencode.ai/zen/go/v1")
        result = llm._create_chat_result(_mk_response_dict("deepseek-v4.1-flash"))
        out = result.llm_output
        assert out["base_url"] == "https://opencode.ai/zen/go/v1"
        assert "prefix_sha256" in out and "prefix_bytes" in out