"""review_service 轻量导入回归：模块级不得拉入 langchain / eval.review 重依赖。"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

_CHECK = (
    "import sys; import server.review_service; "
    "assert 'langchain_core' not in sys.modules and 'eval.review' not in sys.modules, "
    "[m for m in sys.modules if m.startswith(('langchain','eval.review'))]"
)


def test_review_service_import_is_light():
    result = subprocess.run(
        [sys.executable, "-c", _CHECK],
        cwd=ROOT, text=True, encoding="utf-8", errors="replace",
        capture_output=True, check=False, timeout=120,
    )
    assert result.returncode == 0, result.stderr
