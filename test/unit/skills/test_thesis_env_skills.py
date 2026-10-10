"""台账类技能（hang_seng_env / bond_env / dividend_env）的接线与契约测试。

三个技能共用 `tools/thesis_skill_base.py::ThesisSkill`——台账 id 是唯一差异，
因子/阈值/信号全部来自 `data/thesis/*.json`。所以这里用同一套断言跑三遍，
顺带钉住「技能层不自己实现因子逻辑」这条边界。
"""
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
SKILLS = ROOT / "tools" / "skills"

# (目录, 类名, THESIS_ID, 主工具名, describe 工具名, 错误文案前缀)
SKILL_CASES = [
    ("dividend_env", "DividendEnvSkill", "dividend_env",
     "get_dividend_env", None, "红利环境因子"),
    ("hang_seng_env", "HangSengTechEnvSkill", "hang_seng_tech",
     "get_hang_seng_env", "describe_hang_seng_thesis", "恒生科技环境因子"),
    ("bond_env", "BondFundEnvSkill", "bond_fund",
     "get_bond_fund_env", "describe_bond_fund_thesis", "债基环境因子"),
    ("gold_env", "GoldRegimeEnvSkill", "gold_regime",
     "get_gold_env", "describe_gold_thesis", "黄金阶段研判"),
    ("cu_env", "CuRegimeEnvSkill", "cu_regime",
     "get_cu_env", "describe_cu_thesis", "铜周期研判"),
    ("hog_env", "HogRegimeEnvSkill", "hog_regime",
     "get_hog_env", "describe_hog_thesis", "猪周期研判"),
]


def _load(mod_name: str):
    p = SKILLS / mod_name / "main.py"
    spec = importlib.util.spec_from_file_location(mod_name + "_main", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("dir_name,cls_name,tid,tool,dtool,err_label", SKILL_CASES)
class TestThesisSkillContract:
    def test_build_tools_registered(self, dir_name, cls_name, tid, tool, dtool, err_label):
        tools = _load(dir_name).build_tools(MagicMock(), None)
        names = {t.name for t in tools}
        assert tool in names, names
        if dtool:
            assert dtool in names, names

    def test_thesis_id_matches_directory_intent(self, dir_name, cls_name, tid, tool, dtool, err_label):
        assert _load(dir_name).THESIS_ID == tid

    def test_definition_file_exists(self, dir_name, cls_name, tid, tool, dtool, err_label):
        assert (ROOT / "data" / "thesis" / ("%s.json" % tid)).exists()

    def test_engine_is_the_real_thesis_engine(self, dir_name, cls_name, tid, tool, dtool, err_label):
        """技能层不自己实现因子逻辑，必须委托 thesis.engine。"""
        mod = _load(dir_name)
        skill = getattr(mod, cls_name)(MagicMock(), None)
        from thesis.engine import ThesisEngine
        assert isinstance(skill._engine, ThesisEngine)

    def test_compute_delegates_with_its_own_thesis_id(self, dir_name, cls_name, tid, tool, dtool, err_label):
        mod = _load(dir_name)
        skill = getattr(mod, cls_name)(MagicMock(), None)
        seen = {}
        skill._engine.compute = lambda t: (seen.update(thesis_id=t) or
                                           {"thesis_id": t, "total": 3})
        out = json.loads(getattr(skill, tool)())
        assert seen["thesis_id"] == tid
        assert out["total"] == 3

    def test_failure_returns_structured_error_with_hint(self, dir_name, cls_name, tid, tool, dtool, err_label):
        mod = _load(dir_name)
        skill = getattr(mod, cls_name)(MagicMock(), None)

        def boom(t):
            raise RuntimeError("接口不可达")

        skill._engine.compute = boom
        out = json.loads(getattr(skill, tool)())
        assert "error" in out and "hint" in out
        assert err_label in out["error"], out["error"]

    def test_result_is_json_string(self, dir_name, cls_name, tid, tool, dtool, err_label):
        """@skill_tool 契约：返回 JSON 字符串，供 agent 工具层消费。"""
        mod = _load(dir_name)
        skill = getattr(mod, cls_name)(MagicMock(), None)
        skill._engine.compute = lambda t: {"thesis_id": t}
        out = getattr(skill, tool)()
        assert isinstance(out, str)
        assert json.loads(out)["thesis_id"] == tid

    def test_skill_md_exists_and_declares_the_tool_names(self, dir_name, cls_name, tid, tool, dtool, err_label):
        """技能目录缺 SKILL.md 会被注册器**静默跳过**（_load_internal_skill return None），
        且 SKILL.md 里工具名要与 @skill_tool 方法名一致，否则 agent 调不到。"""
        md = (SKILLS / dir_name / "SKILL.md").read_text(encoding="utf-8")
        assert md.startswith("---")
        assert ("name: %s" % dir_name) in md
        assert tool in md, "SKILL.md 未声明工具 %s" % tool
        if dtool:
            assert dtool in md


class TestDescribeTool:
    @pytest.mark.parametrize("dir_name,cls_name,tid,dtool", [
        ("hang_seng_env", "HangSengTechEnvSkill", "hang_seng_tech", "describe_hang_seng_thesis"),
        ("bond_env", "BondFundEnvSkill", "bond_fund", "describe_bond_fund_thesis"),
    ])
    def test_describe_reads_factors_from_definition(self, dir_name, cls_name, tid, dtool):
        """describe 的因子清单必须来自定义 JSON（动态），不能硬编码——
        否则「定义改了、技能文档没改」的口径漂移会重现。"""
        mod = _load(dir_name)
        skill = getattr(mod, cls_name)(MagicMock(), None)
        out = json.loads(getattr(skill, dtool)())
        assert out["thesis_id"] == tid
        assert out["name"]
        assert out["factors"], "因子清单为空"

        defn = json.loads((ROOT / "data" / "thesis" / ("%s.json" % tid)).read_text(encoding="utf-8"))
        assert [f["key"] for f in out["factors"]] == [f["key"] for f in defn["factors"]]
        assert not out["problems"], out["problems"]


class TestThesisSkillBase:
    """基类的两个内部方法（compute_env / describe）返回 **dict**；
    只有 @skill_tool 装饰过的包装方法才返回 JSON 字符串（由装饰器序列化）。"""

    def test_subclass_without_thesis_id_raises(self):
        from tools.thesis_skill_base import ThesisSkill

        class Bad(ThesisSkill):
            pass

        with pytest.raises(ValueError, match="THESIS_ID"):
            Bad(MagicMock(), None)

    def test_internal_methods_return_dicts_not_strings(self):
        from tools.thesis_skill_base import ThesisSkill

        class Plain(ThesisSkill):
            THESIS_ID = "dividend_env"

        skill = Plain(MagicMock(), None)
        skill._engine.compute = lambda t: {"thesis_id": t}
        assert isinstance(skill.compute_env(), dict)
        assert isinstance(skill.describe(), dict)

    def test_error_label_falls_back_to_thesis_id(self):
        from tools.thesis_skill_base import ThesisSkill

        class Plain(ThesisSkill):
            THESIS_ID = "dividend_env"

        skill = Plain(MagicMock(), None)
        skill._engine.compute = lambda t: (_ for _ in ()).throw(RuntimeError("x"))
        out = skill.compute_env()
        assert out["error"].startswith("dividend_env计算失败"), out["error"]
        assert out["hint"]

    def test_error_label_override_changes_message(self):
        from tools.thesis_skill_base import ThesisSkill

        class Named(ThesisSkill):
            THESIS_ID = "bond_fund"
            ERROR_LABEL = "债基环境因子"

        skill = Named(MagicMock(), None)
        skill._engine.compute = lambda t: (_ for _ in ()).throw(RuntimeError("x"))
        assert skill.compute_env()["error"].startswith("债基环境因子计算失败")

    def test_describe_reports_missing_definition(self):
        from tools.thesis_skill_base import ThesisSkill

        class Ghost(ThesisSkill):
            THESIS_ID = "no_such_thesis_xyz"

        out = Ghost(MagicMock(), None).describe()
        assert "error" in out

    def test_base_lives_in_tools_package_not_skills_dir(self):
        """`tools/skills/` 刻意不是包（技能按文件路径加载），基类必须放 tools/ 下。"""
        assert (ROOT / "tools" / "thesis_skill_base.py").exists()
        # skills 目录里不应再留基类副本
        assert not (SKILLS / "_thesis_base.py").exists()
