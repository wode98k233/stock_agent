"""Skill 管理路由契约测试。"""
import asyncio

import pytest
from fastapi import HTTPException


class _MissingSkillRegistry:
    def get_all_skills_unchecked(self):
        return {}

    def rescan(self):
        return None

    def get_skill_unchecked(self, skill_name):
        return None


def test_import_external_package_fails_when_rescan_cannot_find_skill(monkeypatch):
    from server.routes.skills import import_skill
    from server.schemas import SkillImportRequest
    from server.skill_importer import ImportPreview

    preview = ImportPreview(
        status="ok",
        detected_type="external_package",
        skill_name="sample_pack",
        tools=["sample_child"],
    )
    monkeypatch.setattr("server.routes.skills._get_reg", lambda request: _MissingSkillRegistry())
    monkeypatch.setattr("server.skill_importer.preview_import", lambda *args, **kwargs: preview)
    monkeypatch.setattr("server.skill_importer.install_skill", lambda *args, **kwargs: "user_skills/sample_pack")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(import_skill(SkillImportRequest(zip_path="sample.zip"), object()))

    assert exc_info.value.status_code == 500
    assert "sample_child" in str(exc_info.value.detail)


def test_import_external_package_without_loaders_does_not_report_success(monkeypatch):
    from server.routes.skills import import_skill
    from server.schemas import SkillImportRequest
    from server.skill_importer import ImportPreview

    preview = ImportPreview(
        status="warning",
        detected_type="external_package",
        skill_name="empty_pack",
        tools=[],
    )
    monkeypatch.setattr("server.routes.skills._get_reg", lambda request: _MissingSkillRegistry())
    monkeypatch.setattr("server.skill_importer.preview_import", lambda *args, **kwargs: preview)
    monkeypatch.setattr("server.skill_importer.install_skill", lambda *args, **kwargs: "user_skills/empty_pack")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(import_skill(SkillImportRequest(zip_path="empty.zip"), object()))

    assert exc_info.value.status_code == 500
    assert "empty_pack" in str(exc_info.value.detail)
