"""eval 页对话评审前端接线静态断言。"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_review_tab_in_index_html():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'data-tab="review"' in html
    assert "switchTab('review')" in html
    # 版本号会随改动递增，断言用版本无关正则
    assert re.search(r"eval-manager\.js\?v=\d+", html)
    assert re.search(r"eval\.css\?v=\d+", html)


def test_review_functions_in_eval_manager():
    js = (ROOT / "server" / "static" / "js" / "eval-manager.js").read_text(encoding="utf-8")
    # 原型五视图（§17 三期）：评测矩阵 / 题库回归 / 存量回测 / Agent 研判 / 维度注册表
    for fn in ("function renderMatrix", "function renderRegression",
               "function renderExisting", "function renderJudge",
               "function renderRegistry", "function renderDialogReview"):
        assert fn in js, fn
    assert "function startDialogReview" in js
    assert "function refreshDialogReview" in js
    assert "function switchReviewVersion" in js
    assert "/api/eval/reviews" in js
    assert "selectReviewDialog" in js


def test_review_styles_present():
    css = (ROOT / "server" / "static" / "css" / "eval.css").read_text(encoding="utf-8")
    assert ".eval-review-summary" in css
    assert ".eval-review-turn" in css
    assert ".eval-review-bar" in css
