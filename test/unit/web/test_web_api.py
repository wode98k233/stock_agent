from types import SimpleNamespace

_MOCK_AGENT = SimpleNamespace(display_name="Test Agent", description="测试 Agent")


def _patch_agent_factory(monkeypatch, names):
    """同时 mock AgentFactory.list 和 AgentFactory.get"""
    monkeypatch.setattr("agents.factory.AgentFactory.list", classmethod(lambda cls: names))
    monkeypatch.setattr("agents.factory.AgentFactory.get", classmethod(lambda cls, name=None: _MOCK_AGENT))


def test_web_api_dialog_message_and_task_flow(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from server.agent_runner import AgentRunResult
    from server.app import create_app
    from server.runtime import TaskRuntime
    from server.storage import WebStorage
    from utils.progress import ProgressEvent, ProgressType

    async def fake_runner(*, user_input, mode, context, progress_callback):
        progress_callback(ProgressEvent(ProgressType.CLASSIFIER, "分类完成", timestamp=1.0))
        return AgentRunResult(
            response=f"{mode}: {user_input}",
            log_uuid="log-api",
            log_file="logs/log-api.log",
        )

    state = SimpleNamespace(
        storage=WebStorage(tmp_path / "web.db"),
        runtime=TaskRuntime(fake_runner),
        agent_context=SimpleNamespace(),
    )

    _patch_agent_factory(monkeypatch, ["react_stock", "pdor"])
    # Mock 掉 Agent / Memory 就绪门禁，避免 503（测试不走 bootstrap）
    monkeypatch.setattr("server.routes.meta.is_agent_ready", lambda: True)
    monkeypatch.setattr("server.routes.meta.memory_status", lambda: {"ready": True, "warming": False})
    app = create_app(state)

    with TestClient(app) as client:
        modes = client.get("/api/modes")
        assert modes.status_code == 200
        assert [item["name"] for item in modes.json()["modes"]] == ["react_stock", "pdor"]

        dialog_res = client.post("/api/dialogs", json={"title": "测试对话", "mode": "pdor"})
        assert dialog_res.status_code == 200
        dialog_uuid = dialog_res.json()["dialog_uuid"]

        submit_res = client.post(
            f"/api/dialogs/{dialog_uuid}/messages",
            json={"content": "分析宁德时代", "mode": "pdor"},
        )
        assert submit_res.status_code == 200
        task_id = submit_res.json()["task_id"]

        with client.stream("GET", f"/api/tasks/{task_id}/events") as stream:
            body = "".join(stream.iter_text())

        assert "event: progress" in body
        assert "event: final" in body
        assert "pdor: 分析宁德时代" in body

        messages = client.get(f"/api/dialogs/{dialog_uuid}/messages").json()["items"]
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"
        assert messages[1]["status"] == "completed"
        assert messages[1]["content"] == "pdor: 分析宁德时代"


def test_web_config_exposes_report_min_tokens(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from server.app import create_app
    from server.runtime import TaskRuntime
    from server.storage import WebStorage

    async def fake_runner(*, user_input, mode, context, progress_callback):
        raise AssertionError("not used")

    state = SimpleNamespace(
        storage=WebStorage(tmp_path / "web.db"),
        runtime=TaskRuntime(fake_runner),
        agent_context=SimpleNamespace(),
    )

    _patch_agent_factory(monkeypatch, ["react_stock"])
    app = create_app(state)

    with TestClient(app) as client:
        res = client.get("/api/config")

    assert res.status_code == 200
    items = res.json()["items"]
    item = next((i for i in items if i["key"] == "REPORT_MIN_TOKENS"), None)
    assert item is not None
    assert item["type"] == "int"
    assert item["group"] == "报告"


def test_modes_endpoint_reuses_cached_payload(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from server.app import create_app
    from server.runtime import TaskRuntime
    from server.storage import WebStorage

    async def fake_runner(*, user_input, mode, context, progress_callback):
        raise AssertionError("not used")

    calls = {"count": 0}

    def fake_list(cls):
        calls["count"] += 1
        return ["react_stock", "pdor"]

    monkeypatch.setattr("agents.factory.AgentFactory.list", classmethod(fake_list))
    monkeypatch.setattr("agents.factory.AgentFactory.get", classmethod(lambda cls, name=None: _MOCK_AGENT))
    # 清除模块级缓存，避免上一个测试残留
    import server.routes.meta as meta_mod
    monkeypatch.setattr(meta_mod, "_modes_payload_cache", None)
    state = SimpleNamespace(
        storage=WebStorage(tmp_path / "web.db"),
        runtime=TaskRuntime(fake_runner),
        agent_context=SimpleNamespace(),
    )
    app = create_app(state)
    calls_after_create = calls["count"]

    with TestClient(app) as client:
        first = client.get("/api/modes")
        second = client.get("/api/modes")

    assert first.status_code == 200
    assert second.status_code == 200
    assert calls["count"] == calls_after_create + 1


def test_datasources_endpoint_uses_fast_status_without_source_probe(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from server.app import create_app
    from server.runtime import TaskRuntime
    from server.storage import WebStorage
    from tools.fetcher.base import DataSourceManager

    async def fake_runner(*, user_input, mode, context, progress_callback):
        raise AssertionError("not used")

    class FakeSource:
        name = "slow_source"
        priority = 99
        enabled = True
        probe_called = False

        @classmethod
        def is_available(cls):
            cls.probe_called = True
            return False

    class FakeCircuitBreaker:
        def get_status(self):
            return {}

        def is_available(self, source_name, capability=None):
            return True

    monkeypatch.setattr(DataSourceManager, "_sources", [FakeSource])
    monkeypatch.setattr(DataSourceManager, "_circuit_breaker", FakeCircuitBreaker())
    _patch_agent_factory(monkeypatch, ["react_stock"])
    # 清除模块级缓存，阻止延迟初始化覆盖 mock
    import server.routes.meta as meta_mod
    monkeypatch.setattr(meta_mod, "_datasources_payload_cache", None)
    import tools.fetcher as fetcher_mod
    monkeypatch.setattr(fetcher_mod, "_initialized", True)

    state = SimpleNamespace(
        storage=WebStorage(tmp_path / "web.db"),
        runtime=TaskRuntime(fake_runner),
        agent_context=SimpleNamespace(),
    )
    app = create_app(state)

    with TestClient(app) as client:
        res = client.get("/api/datasources")

    assert res.status_code == 200
    assert res.json()["sources"][0]["name"] == "slow_source"
    assert res.json()["sources"][0]["available"] is True
    assert FakeSource.probe_called is False


def test_calendar_uses_created_range_query_not_dialog_list_limit(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from server.app import create_app
    from server.runtime import TaskRuntime
    from server.storage import WebStorage

    async def fake_runner(*, user_input, mode, context, progress_callback):
        raise AssertionError("not used")

    storage = WebStorage(tmp_path / "web.db")
    dialog = storage.create_dialog(title="5月研究记录", mode="react_stock")
    storage._conn.execute(
        """
        UPDATE web_dialogs
        SET created_at=?, updated_at=?
        WHERE dialog_uuid=?
        """,
        ("2026-05-15T08:00:00+00:00", "2026-05-15T08:00:00+00:00", dialog["dialog_uuid"]),
    )
    storage._conn.commit()

    def fail_full_dialog_scan(*args, **kwargs):
        raise AssertionError("calendar must not scan list_dialogs(limit=5000)")

    storage.list_dialogs = fail_full_dialog_scan

    state = SimpleNamespace(
        storage=storage,
        runtime=TaskRuntime(fake_runner),
        agent_context=SimpleNamespace(),
    )

    _patch_agent_factory(monkeypatch, ["react_stock"])
    app = create_app(state)

    with TestClient(app) as client:
        res = client.get("/api/calendar?year=2026&month=5")

    assert res.status_code == 200
    body = res.json()
    assert body["dialog_days"]["2026-05-15"][0]["dialog_uuid"] == dialog["dialog_uuid"]


def test_report_templates_api_lists_reads_and_saves_template(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from server.app import create_app
    from server.runtime import TaskRuntime
    from server.storage import WebStorage

    async def fake_runner(*, user_input, mode, context, progress_callback):
        raise AssertionError("not used")

    root = tmp_path / "report_templates"
    (root / "standard").mkdir(parents=True)
    (root / "blocks").mkdir()
    (root / "index.json").write_text(
        """
        {
          "default": "standard",
          "templates": {
            "standard": {
              "name": "标准",
              "description": "默认模板",
              "path": "standard/template.json",
              "enabled": true
            }
          },
          "skills": {"mx_data": {"enabled": true}},
          "capability_mapping": {}
        }
        """,
        encoding="utf-8",
    )
    (root / "standard" / "template.json").write_text(
        """
        {
          "id": "standard",
          "name": "标准分析报告",
          "version": "1.0",
          "role": "分析师",
          "required_skills": ["mx_data"],
          "output_blocks": ["core_summary"],
          "data_contract": [],
          "qa_rules": ["必须有结论"]
        }
        """,
        encoding="utf-8",
    )
    (root / "blocks" / "core_summary.json").write_text(
        '{"id":"core_summary","title":"核心结论","required":true}',
        encoding="utf-8",
    )

    _patch_agent_factory(monkeypatch, ["react_stock"])
    monkeypatch.setattr("server.report_template_service.get_report_templates_root", lambda: root)

    state = SimpleNamespace(
        storage=WebStorage(tmp_path / "web.db"),
        runtime=TaskRuntime(fake_runner),
        agent_context=SimpleNamespace(),
    )
    app = create_app(state)

    with TestClient(app) as client:
        list_res = client.get("/api/report-templates")
        assert list_res.status_code == 200
        body = list_res.json()
        assert body["default"] == "standard"
        assert body["templates"][0]["id"] == "standard"
        assert body["templates"][0]["block_count"] == 1
        assert body["blocks"][0]["id"] == "core_summary"
        assert body["skills"]["mx_data"]["enabled"] is True

        detail_res = client.get("/api/report-templates/standard")
        assert detail_res.status_code == 200
        detail = detail_res.json()
        assert detail["template"]["name"] == "标准分析报告"
        assert detail["entry"]["path"] == "standard/template.json"

        detail["template"]["name"] = "标准分析报告 v2"
        detail["template"]["qa_rules"].append("必须提示风险")
        save_res = client.put("/api/report-templates/standard", json={"template": detail["template"]})
        assert save_res.status_code == 200
        assert save_res.json()["template"]["name"] == "标准分析报告 v2"

        saved_text = (root / "standard" / "template.json").read_text(encoding="utf-8")
        assert '"name": "标准分析报告 v2"' in saved_text
        assert saved_text.endswith("\n")


def test_report_templates_api_rejects_path_traversal(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from server.app import create_app
    from server.runtime import TaskRuntime
    from server.storage import WebStorage

    async def fake_runner(*, user_input, mode, context, progress_callback):
        raise AssertionError("not used")

    root = tmp_path / "report_templates"
    root.mkdir()
    (root / "index.json").write_text(
        """
        {
          "templates": {
            "bad": {
              "name": "bad",
              "description": "bad",
              "path": "../outside.json",
              "enabled": true
            }
          }
        }
        """,
        encoding="utf-8",
    )
    _patch_agent_factory(monkeypatch, ["react_stock"])
    monkeypatch.setattr("server.report_template_service.get_report_templates_root", lambda: root)

    state = SimpleNamespace(
        storage=WebStorage(tmp_path / "web.db"),
        runtime=TaskRuntime(fake_runner),
        agent_context=SimpleNamespace(),
    )
    app = create_app(state)

    with TestClient(app) as client:
        res = client.get("/api/report-templates/bad")

    assert res.status_code == 400
