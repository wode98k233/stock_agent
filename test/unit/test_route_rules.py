import json
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path


class TestRouteRulesService:
    """路由规则 service 层测试。"""

    def test_load_route_rules(self, tmp_path):
        """从 route_rules.json 加载路由规则。"""
        rules = {"test_tpl": [["关键词", 10], ["另一个", 20]]}
        (tmp_path / "route_rules.json").write_text(
            json.dumps(rules, ensure_ascii=False), encoding="utf-8"
        )
        with patch("server.report_template_service.get_report_templates_root", return_value=tmp_path):
            from server.report_template_service import load_route_rules
            result = load_route_rules()
        assert result == rules

    def test_load_route_rules_missing_file(self, tmp_path):
        """文件不存在时返回空 dict。"""
        with patch("server.report_template_service.get_report_templates_root", return_value=tmp_path):
            from server.report_template_service import load_route_rules
            result = load_route_rules()
        assert result == {}

    def test_save_route_rules(self, tmp_path):
        """保存单个模板的路由规则。"""
        existing = {"other": [["x", 1]]}
        (tmp_path / "route_rules.json").write_text(
            json.dumps(existing, ensure_ascii=False), encoding="utf-8"
        )
        with patch("server.report_template_service.get_report_templates_root", return_value=tmp_path):
            from server.report_template_service import save_route_rules
            save_route_rules("test_tpl", [["新词", 15]])

        saved = json.loads((tmp_path / "route_rules.json").read_text(encoding="utf-8"))
        assert saved["test_tpl"] == [["新词", 15]]
        assert saved["other"] == [["x", 1]]

    def test_save_route_rules_creates_file(self, tmp_path):
        """文件不存在时自动创建。"""
        with patch("server.report_template_service.get_report_templates_root", return_value=tmp_path):
            from server.report_template_service import save_route_rules
            save_route_rules("new_tpl", [["词", 5]])

        saved = json.loads((tmp_path / "route_rules.json").read_text(encoding="utf-8"))
        assert saved == {"new_tpl": [["词", 5]]}

    def test_save_route_rules_invalidates_runtime_cache(self, tmp_path):
        """service 保存后，运行时应立即读取新规则。"""
        from agents.analysis import template_store
        from server.report_template_service import save_route_rules

        (tmp_path / "route_rules.json").write_text(
            json.dumps({"test_tpl": [["旧词", 1]]}, ensure_ascii=False),
            encoding="utf-8",
        )
        template_store._route_rules_cache = None
        with patch("agents.analysis.template_store.get_template_dir", return_value=str(tmp_path)):
            assert template_store.get_template_route_rules() == [("test_tpl", [["旧词", 1]])]
            with patch("server.report_template_service.get_report_templates_root", return_value=tmp_path):
                save_route_rules("test_tpl", [["新词", 10]])
            assert template_store.get_template_route_rules() == [("test_tpl", [["新词", 10]])]
        template_store._route_rules_cache = None
