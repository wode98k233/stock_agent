"""静态资源版本号守卫（防 immutable 缓存导致「改了 JS 但老浏览器一年不重取」）。

背景：`/static/**?v=N` 的响应头是 `public, max-age=31536000, immutable`
（`server/cache_headers.py`）。改完 JS/CSS 忘升 `index.html` 的 `?v=N`，老浏览器
会一直用旧文件（2026-11-01 `trading-calendar.js` 漏升 → 日历「深度报告」列永远不出现）。

规则：
1. `index.html` 里每个 `/static/...?v=N` 引用：目标文件必须存在、版本号必须是正整数；
2. git 可用时：若某资源最后一次提交 **晚于** `index.html` 最后一次提交，
   而工作区又没有为它升过版本，则失败（提醒补 `?v=N`）。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
INDEX_REL = "server/static/index.html"
INDEX = ROOT / INDEX_REL
REF_RE = re.compile(r'(?:src|href)="(/static/[^"#?]+)\?v=(\d+)"')


def _refs(html: str | None = None) -> dict[str, int]:
    text = INDEX.read_text(encoding="utf-8") if html is None else html
    return {m.group(1): int(m.group(2)) for m in REF_RE.finditer(text)}


def _git(*args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(ROOT), *args],
            capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
        )
        return proc.returncode, (proc.stdout or "").strip()
    except Exception:  # noqa: BLE001 —— 没装 git / 非仓库 → 调用方 skip
        return 1, ""


def _last_commit(path: str) -> str:
    code, out = _git("log", "-1", "--format=%H", "--", path)
    return out if code == 0 else ""


def _is_ancestor(older: str, newer: str) -> bool:
    code, _ = _git("merge-base", "--is-ancestor", older, newer)
    return code == 0


class TestStaticAssetVersions:
    def test_refs_exist_and_version_positive(self):
        refs = _refs()
        assert refs, "index.html 里没解析出任何带 ?v= 的静态资源"
        missing = []
        for url, version in refs.items():
            assert version > 0, f"{url} 的 ?v= 必须是正整数，实际 {version}"
            if not (ROOT / f"server{url}").is_file():
                missing.append(url)
        assert not missing, f"index.html 引用了不存在的静态资源：{missing}"

    def test_asset_changed_after_index_html_must_bump_version(self):
        code, _ = _git("rev-parse", "--is-inside-work-tree")
        if code != 0:
            pytest.skip("非 git 工作区，跳过版本历史一致性检查")
        code, head = _git("rev-parse", "HEAD")
        if code != 0 or not head:
            pytest.skip("git 无 HEAD（浅克隆/空仓库），跳过版本历史一致性检查")

        index_commit = _last_commit(INDEX_REL)
        if not index_commit:
            pytest.skip("index.html 无提交历史")
        code, head_html = _git("show", f"HEAD:{INDEX_REL}")
        if code != 0:
            pytest.skip("无法读取 HEAD 版 index.html")
        head_versions = _refs(head_html)
        work_versions = _refs()

        stale: list[str] = []
        for url in sorted(work_versions):
            asset_commit = _last_commit(f"server{url}")
            if not asset_commit or asset_commit == index_commit:
                continue  # 与 index.html 同一次提交（正常同改）
            if not _is_ancestor(index_commit, asset_commit):
                continue  # 资源改动不比 index.html 新，说明版本已随某次 index.html 改动升过
            # HEAD 里 index.html 落后于资源改动：工作区必须已经为它升过版本
            if work_versions[url] > head_versions.get(url, 0):
                continue
            stale.append(
                f"{url} 最后改动 {asset_commit[:8]}，晚于 index.html 的 {index_commit[:8]}，"
                f"但 ?v= 仍是 {work_versions[url]}（HEAD 也是 {head_versions.get(url) or '无'}）"
            )

        assert not stale, (
            "以下静态资源改过但没升 index.html 的 ?v=：\n  " + "\n  ".join(stale)
            + "\n带 v 的资源是 immutable（max-age=31536000），不升版本老浏览器一年内不会重取。"
        )
