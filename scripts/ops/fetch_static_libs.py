#!/usr/bin/env python
"""补齐 `server/static/lib/` 的 5 个第三方前端库（首次运行 / 换机器 / 升级时拉取）。

## 背景

`server/static/lib/` 下 5 个前端库合计约 3.5MB（光 `mermaid.min.js` 就 3.3MB），
作者明确**故意不入库**：进 git 会成为永久历史负担。根 `.gitignore` 的 `lib/`
规则（无锚定）正好把它们一起忽略，当前状态即"不入库、需要时拉取"。

缺了会静默降级（v2.1 公开版已知缺陷）：

| 缺失文件 | 前端表现 |
|---|---|
| `marked.min.js` | 对话 markdown 不渲染，退化为纯文本 + 换行 |
| `highlight.min.js` | 代码块无高亮 |
| `mermaid.min.js` | mermaid 图不渲染（`_ensureMermaid` 静默 return false） |
| `html-to-image.js` | 对话导出图片失败（报"导出依赖未就绪 / html-to-image 未加载"） |
| `github-dark.min.css` | 代码块高亮配色丢失（不影响功能） |

本脚本按**锁定版本**下载到 `server/static/lib/`，并用 sha256 校验；已存在且校验通过
则跳过（幂等），`--force` 强制重下，`--check` 只体检不下载。

## 版本来源（2026-11-15 用仓库内现有文件反向确认，未凭猜测）

| 文件 | 锁定版本 | 版本依据 |
|---|---|---|
| `marked.min.js` | 12.0.2 | 文件头注释 `marked v12.0.2` |
| `highlight.min.js` | 11.9.0 | 文件头注释 `Highlight.js v11.9.0 (git: f47103d4f1)` |
| `mermaid.min.js` | 10.9.3 | UMD 包内无 bannner；内嵌 `const ...="10.9.3"` 为 mermaid 的 version 常量，且与 unpkg `mermaid@10.9.3/dist/mermaid.min.js` **sha256 完全一致** |
| `html-to-image.js` | 1.11.11 | 包内无版本字符串（UMD 无 banner），按 sha256 反查 npm 各版本：与 `1.11.11`（及字节相同的 `1.11.9`）一致；锁定较新的 `1.11.11` |
| `github-dark.min.css` | 11.9.0（highlight.js 内置主题） | highlight.js 11.8.0~11.11.1 的该主题字节完全相同，取配套的 11.9.0；**仓库内本地副本经过格式处理**（见下） |

### 关于 `github-dark.min.css` 的本地差异（不是下载错误）

仓库里现有的本地副本 sha256 = `ba6327de…`，官方 11.9.0 是 `9f208d02…`。逐字节 diff
只有两处、且都是格式差异：

1. 头部块注释里少了一个空行（`2021-05-15` 与 `Outdated base version` 之间）；
2. 文件末尾多了一个 `\\n`。

两者长度都是 1315B，CSS 规则与配色**完全等价**，渲染无差别。本脚本写入的是**官方字节**，
同时在 `alt_sha256` 里登记这个已知本地变体（`--check` 会提示是变体但不算失败）；
`--force` 会把本地变体覆盖成官方字节。

## 用法

    # 检查（不联网、不下载；CI / 服务启动自检可用）
    .\\venv\\Scripts\\python.exe scripts\\ops\\fetch_static_libs.py --check

    # 补齐缺失文件（已存在且校验通过会跳过，可重复执行）
    .\\venv\\Scripts\\python.exe scripts\\ops\\fetch_static_libs.py

    # 强制全部重下
    .\\venv\\Scripts\\python.exe scripts\\ops\\fetch_static_libs.py --force

    # 打印锁定清单（版本 / 大小 / sha256 / 上游地址）
    .\\venv\\Scripts\\python.exe scripts\\ops\\fetch_static_libs.py --list

    # 内网镜像优先（镜像按"文件名"直接取：<mirror>/marked.min.js）
    ... --mirror https://static.example.com/lib

    # 只用镜像、完全不走公网 CDN（离线内网 / 测试离线分支）
    ... --mirror https://static.example.com/lib --no-cdn

下载顺序：`--mirror`（若有）→ unpkg → jsDelivr → npmmirror（国内可用）。
单文件校验失败会换下一个源；所有源都失败则给出可手动放置的清单（见文末提示）。

## 文件放在哪里 / 谁在用

- 目标目录：`server/static/lib/`（`--dir` 可改）
- 引用方：`server/static/index.html`、`server/static/js/chat-markdown.js`、
  `server/static/js/chat-export.js`、`server/static/js/short-line.js`
- 说明文档（入库）：`server/static/LIB-README.md`

## 退出码

- `0`：全部就绪（含"本地已知变体"）
- `1`：有文件缺失 / 校验不符 / 下载失败

只用标准库，无需装依赖；不对 git 做任何操作。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = ROOT / "server" / "static" / "lib"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 StockRadar-fetch-static-libs/1.0")


# ─────────────────────────────────────────────────────────────
# 锁定清单：改版本 = 改这里，同时更新 sha256 与 LIB-README.md
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Lib:
    """一个待补齐的静态库。"""

    name: str            # 落地文件名（必须与前端引用一致）
    pkg: str             # npm 包名（scoped 包带 @scope/）
    version: str         # 锁定版本
    subpath: str         # 包内路径
    size: int            # 官方字节数
    sha256: str          # 官方 sha256
    purpose: str         # 用途（给人看的）
    license: str         # 许可证
    alt_sha256: tuple[str, ...] = ()   # 已知的本地变体（字节不同但语义等价）
    alt_note: str = ""                 # 变体说明


LIBS: tuple[Lib, ...] = (
    Lib(
        name="marked.min.js",
        pkg="marked",
        version="12.0.2",
        subpath="marked.min.js",
        size=35479,
        sha256="15fabce5b65898b32b03f5ed25e9f891a729ad4c0d6d877110a7744aa847a894",
        purpose="对话 markdown 渲染",
        license="MIT",
    ),
    Lib(
        name="mermaid.min.js",
        pkg="mermaid",
        version="10.9.3",
        subpath="dist/mermaid.min.js",
        size=3336760,
        sha256="5a8ec91820bd55afef049068489369910e5d6ce70c8103952f27e29d3e76e8bc",
        purpose="对话里的 mermaid 流程图",
        license="MIT",
    ),
    Lib(
        name="highlight.min.js",
        pkg="@highlightjs/cdn-assets",
        version="11.9.0",
        subpath="highlight.min.js",
        size=121727,
        sha256="837a6fa5b0c736b52bbde2b2b6190f305da3fc9ed41681db5321507057b5c846",
        purpose="代码块语法高亮",
        license="BSD-3-Clause",
    ),
    Lib(
        name="html-to-image.js",
        pkg="html-to-image",
        version="1.11.11",
        subpath="dist/html-to-image.js",
        size=19568,
        sha256="0181b9a4ea3351540751b2e72b6baecb5c2297093fcb0bd2af94fc531cb0fbda",
        purpose="对话 / 短线面板导出图片",
        license="MIT",
    ),
    Lib(
        name="github-dark.min.css",
        pkg="@highlightjs/cdn-assets",
        version="11.9.0",
        subpath="styles/github-dark.min.css",
        size=1315,
        sha256="9f208d022102b1d0c7aebfecd8e42ca7997d5de636649d2b31ea63093d809019",
        purpose="代码块深色配色（highlight.js 主题）",
        license="BSD-3-Clause",
        alt_sha256=("ba6327de51b22a20289bd1896f3e8012abd0a1771f0e93442d0a3aca5af12d09",),
        alt_note="本地历史副本：块注释少一空行 + 末尾多一换行，CSS 语义与官方等价",
    ),
)

_MIRROR_TEMPLATES = (
    "https://unpkg.com/{pkg}@{version}/{subpath}",
    "https://cdn.jsdelivr.net/npm/{pkg}@{version}/{subpath}",
    "https://registry.npmmirror.com/{pkg}/{version}/files/{subpath}",
)


# ── 控制台编码适配：管道/CI 走 UTF-8，老式 cp936 控制台退化为 ASCII 标记 ──
OK, NG, WARN, DONE, FAIL = "✓", "✗", "⚠", "✅", "❌"


def _configure_output() -> None:
    """管道输出统一 UTF-8（避免 GBK 编不出 ✓ 直接抛 UnicodeEncodeError）。"""
    global OK, NG, WARN, DONE, FAIL
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        "✓⚠✅❌".encode(enc)
    except (UnicodeEncodeError, LookupError):
        OK, NG, WARN, DONE, FAIL = "[OK]", "[XX]", "[!]", "[DONE]", "[FAIL]"


class IntegrityError(Exception):
    """下载到的字节数与/或 sha256 与锁定值不符。"""

    def __init__(self, lib: Lib, url: str, size: int, sha: str):
        self.lib = lib
        self.url = url
        self.size = size
        self.sha = sha
        super().__init__(
            f"{lib.name}: 下载内容校验失败（源 {url}）；"
            f"实际 {size}B / sha256={sha[:16]}…，期望 {lib.size}B / sha256={lib.sha256[:16]}…"
        )


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def human_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.2f}MB"
    return f"{n / 1024:.0f}KB"


def build_urls(lib: Lib, mirrors: list[str], use_cdn: bool) -> list[str]:
    """按优先级给出候选地址：显式镜像 → unpkg → jsDelivr → npmmirror。"""
    urls = [f"{m.rstrip('/')}/{lib.name}" for m in mirrors]
    if use_cdn:
        urls.extend(
            tpl.format(pkg=lib.pkg, version=lib.version, subpath=lib.subpath)
            for tpl in _MIRROR_TEMPLATES
        )
    return urls


def download(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": _UA, "Accept": "*/*", "Accept-Encoding": "identity"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 固定 https 源
        return resp.read()


def fetch(lib: Lib, dest: Path, urls: list[str], timeout: float,
          quiet: bool = False) -> str:
    """依次尝试候选源，校验通过后原子落盘；返回成功使用的 URL。

    某个源内容校验不过（中间人 / 错误版本 / 截断）只跳过该源，继续试下一个；
    全部失败时优先抛 IntegrityError（说明拿到了东西但不对），否则抛 RuntimeError。
    """
    errors: list[str] = []
    bad_content: IntegrityError | None = None
    for url in urls:
        if not quiet:
            print(f"    >> {url}", flush=True)
        started = time.time()
        try:
            data = download(url, timeout)
        except urllib.error.HTTPError as exc:
            errors.append(f"{url} → HTTP {exc.code}")
            if not quiet:
                print(f"      x HTTP {exc.code}，换下一个源", flush=True)
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            errors.append(f"{url} → {type(exc).__name__}: {reason}")
            if not quiet:
                print(f"      x 连接失败（{reason}），换下一个源", flush=True)
            continue

        actual_sha = sha256_of(data)
        if len(data) != lib.size or actual_sha != lib.sha256:
            bad_content = IntegrityError(lib, url, len(data), actual_sha)
            errors.append(f"{url} → 校验不符（{len(data)}B / {actual_sha[:16]}…）")
            if not quiet:
                print(f"      x 内容校验不符（{len(data)}B / {actual_sha[:12]}…），换下一个源",
                      flush=True)
            continue

        tmp = dest.with_name(dest.name + ".part")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        if not quiet:
            print(
                f"      {OK} 已写入 {lib.name}"
                f"（{human_size(len(data))}，{time.time() - started:.1f}s）",
                flush=True,
            )
        return url

    if bad_content is not None:
        raise bad_content
    raise RuntimeError(f"{lib.name}: 所有下载源均失败\n      " + "\n      ".join(errors))


def probe(lib: Lib, dest: Path) -> tuple[str, str]:
    """检查现有文件状态。

    返回 (状态, 说明)，状态取值：
    - ``ok``      已存在且是官方字节
    - ``alt``     已存在，是登记过的本地等价变体
    - ``mismatch``已存在但 sha256 不在锁定 / 变体清单里
    - ``missing`` 不存在
    """
    if not dest.exists():
        return "missing", "文件不存在"
    size = dest.stat().st_size
    digest = sha256_file(dest)
    if digest == lib.sha256:
        return "ok", f"{human_size(size)}，sha256 校验通过"
    if digest in lib.alt_sha256:
        return "alt", f"{human_size(size)}，sha256={digest[:12]}… 为已知本地变体（{lib.alt_note}）"
    return "mismatch", f"{human_size(size)}，sha256={digest[:12]}… 与锁定值 {lib.sha256[:12]}… 不符"


def _keep_or_fail(lib: Lib, dest: Path, record: dict, failed: list[Lib],
                  *, quiet: bool = False) -> bool:
    """刷新/下载失败后复核本地文件：仍然有效就只告警，不把可用安装判成未就绪。

    返回 True 表示「本地文件仍有效、仅本次刷新失败」。
    """
    status, detail = probe(lib, dest)
    record.update(status=status, detail=detail)
    if status in ("ok", "alt"):
        if not quiet:
            print(f"    {WARN} 本次刷新失败，但本地文件仍有效，保持不动")
        return True
    failed.append(lib)
    return False


def manual_hints(libs: list[Lib], target: Path) -> str:
    """离线 / 全源失败时的手动放置说明。"""
    lines = [
        "",
        "=" * 78,
        f"{WARN}  无法通过脚本补齐，请按下面步骤手动放置（离线也可完成）：",
        "=" * 78,
        "1) 在能上网的机器上，按「文件名 → 下载地址」逐个下载（文件名必须完全一致）：",
    ]
    for lib in libs:
        lines.append(f"   {lib.name:<20} {lib.version:<8} "
                     f"https://unpkg.com/{lib.pkg}@{lib.version}/{lib.subpath}")
    dir_args = "" if target == DEFAULT_DIR.resolve() else f' --dir "{target}"'
    lines.extend([
        f"2) 把下载到的文件拷贝到：{target}",
        "   （目录不存在就先建；不要改文件名、不要解压）",
        "3) 回来校验（不联网）：",
        f"   {sys.executable} scripts/ops/fetch_static_libs.py --check{dir_args}",
        "",
        "   离线但有内网文件服务时，也可以：",
        f"   {sys.executable} scripts/ops/fetch_static_libs.py "
        f"--mirror http://<内网地址>/lib --no-cdn{dir_args}",
        "=" * 78,
    ])
    return "\n".join(lines)


def print_manifest() -> None:
    print(f"{'文件':<20} {'版本':<8} {'大小':>8}  sha256（前 16）  用途")
    print("-" * 92)
    for lib in LIBS:
        print(f"{lib.name:<20} {lib.version:<8} {human_size(lib.size):>8}  "
              f"{lib.sha256[:16]}  {lib.purpose}")
    print("-" * 92)
    print("来源：https://unpkg.com/{pkg}@{version}/{subpath}（jsDelivr / npmmirror 同内容）")
    print("说明文档：server/static/LIB-README.md")


def main(argv: list[str] | None = None) -> int:
    _configure_output()
    ap = argparse.ArgumentParser(
        description="按锁定版本补齐 server/static/lib/ 的第三方前端库（幂等 + sha256 校验）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python scripts/ops/fetch_static_libs.py --check\n"
               "  python scripts/ops/fetch_static_libs.py\n"
               "  python scripts/ops/fetch_static_libs.py --force\n",
    )
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help="目标目录（默认 server/static/lib）")
    ap.add_argument("--check", action="store_true", help="只校验现有文件，不联网、不下载")
    ap.add_argument("--force", action="store_true", help="已存在也重新下载并覆盖")
    ap.add_argument("--list", action="store_true", help="打印锁定清单后退出")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果（便于服务启动自检解析）")
    ap.add_argument("--timeout", type=float, default=60.0, help="单次下载超时秒数（默认 60）")
    ap.add_argument("--mirror", action="append", default=[], metavar="URL",
                    help="额外镜像前缀，按「文件名」直接取（可重复）")
    ap.add_argument("--no-cdn", action="store_true", help="只用 --mirror，不访问公网 CDN")
    args = ap.parse_args(argv)

    if args.no_cdn and not args.mirror:
        ap.error("--no-cdn 至少需要一个 --mirror")
    if args.check and args.force:
        ap.error("--check 与 --force 不能同时使用（一个只读、一个重下）")

    if args.list:
        print_manifest()
        return 0

    target = Path(args.dir).expanduser().resolve()
    if not target.exists() and not args.check:
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"{NG} 无法创建目录 {target}：{exc}", file=sys.stderr)
            return 1

    results: list[dict] = []
    failed: list[Lib] = []
    network_errors = 0
    refresh_kept = 0

    if not args.json:
        print(f"目标目录：{target}")
        print(f"模式：{'仅校验' if args.check else ('强制重下' if args.force else '按需补齐')}"
              f"｜共 {len(LIBS)} 个文件，合计 {human_size(sum(x.size for x in LIBS))}")
        print("")

    for lib in LIBS:
        dest = target / lib.name
        status, detail = probe(lib, dest)
        record = {
            "name": lib.name,
            "version": lib.version,
            "path": str(dest),
            "status": status,
            "detail": detail,
            "sha256": lib.sha256,
        }

        if not args.json:
            print(f"[{lib.name}] 锁定 {lib.version}（{human_size(lib.size)}）— {lib.purpose}")

        if status == "ok" and not args.force:
            if not args.json:
                print(f"    {OK} 已存在，{detail}，跳过")
            record["action"] = "skip"
            results.append(record)
            continue

        if status == "alt" and not args.force:
            if not args.json:
                print(f"    {WARN} 已存在但是本地变体：{detail}")
                print("      → 保持不动（渲染等价）；如需换成官方字节加 --force")
            record["action"] = "skip-alt"
            results.append(record)
            continue

        if status == "mismatch" and not args.force:
            if args.check:
                if not args.json:
                    print(f"    {NG} {detail}（--check 不下载；补齐：python "
                          "scripts/ops/fetch_static_libs.py）")
                record["action"] = "invalid"
                failed.append(lib)
                results.append(record)
                continue
            if not args.json:
                print(f"    {NG} {detail}")
                print("      → 默认不覆盖；确认要覆盖请加 --force，或先手工核对来源")
            record["action"] = "mismatch"
            failed.append(lib)
            results.append(record)
            continue

        if args.check:
            if not args.json:
                print(f"    {NG} {detail}（--check 不下载）")
            record["action"] = "missing" if status == "missing" else "invalid"
            failed.append(lib)
            results.append(record)
            continue

        urls = build_urls(lib, args.mirror, not args.no_cdn)
        try:
            used = fetch(lib, dest, urls, args.timeout, quiet=args.json)
        except IntegrityError as exc:
            if not args.json:
                print(f"    {NG} {exc}")
            record.update(action="integrity-failed", error=str(exc))
            refresh_kept += _keep_or_fail(lib, dest, record, failed, quiet=args.json)
            results.append(record)
            continue
        except RuntimeError as exc:
            if not args.json:
                print(f"    {NG} {exc}")
            network_errors += 1
            record.update(action="network-failed", error=str(exc))
            refresh_kept += _keep_or_fail(lib, dest, record, failed, quiet=args.json)
            results.append(record)
            continue
        except OSError as exc:
            # 落盘失败（只读目录 / 权限 / 磁盘满）
            if not args.json:
                print(f"    {NG} 写入失败 {dest}：{exc}")
            record.update(action="write-failed", error=str(exc))
            refresh_kept += _keep_or_fail(lib, dest, record, failed, quiet=args.json)
            results.append(record)
            continue

        record.update(action="downloaded", source=used)
        results.append(record)

        # 防御：确认落盘后的字节确实是锁定的那份（并用复核结果刷新汇总行）
        after_status, after_detail = probe(lib, dest)
        record.update(status=after_status, detail=after_detail)
        if after_status != "ok":
            if not args.json:
                print(f"    {NG} 落盘后复核失败：{after_detail}")
            record.update(action="verify-failed")
            failed.append(lib)

    ok_count = sum(1 for r in results if r["status"] == "ok" or r["status"] == "alt")

    if args.json:
        print(json.dumps(
            {
                "target": str(target),
                "ok": not failed,
                "okCount": ok_count,
                "total": len(LIBS),
                "files": results,
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0 if not failed else 1

    print("")
    print("-" * 78)
    for r in results:
        mark = OK if r["status"] in ("ok", "alt") else NG
        extra = ""
        if r.get("source"):
            extra = f"  ← {r['source']}"
        elif r.get("error"):
            extra = f"  ({r['error'].splitlines()[0]})"
        print(f" {mark} {r['name']:<20} {r['version']:<8} {r['detail']}{extra}")
    print("-" * 78)

    if not failed:
        print(f"{DONE} 就绪：{ok_count}/{len(LIBS)} 个静态库通过校验；"
              "对话 markdown / 代码高亮 / mermaid / 导出图片均可用。")
        if refresh_kept:
            print(f"   {WARN} 其中 {refresh_kept} 个文件本次刷新失败（网络/写盘），"
                  "本地文件仍有效、未被改动")
        if any(r["status"] == "alt" for r in results):
            print("   提示：有本地变体文件（语义等价）。想让它们变成官方字节：--force")
        return 0

    print(f"{FAIL} 未就绪：{len(failed)}/{len(LIBS)} 个文件缺失或校验失败："
          f"{', '.join(x.name for x in failed)}")
    print("   受影响功能：对话 markdown 渲染 / 代码高亮 / mermaid 图 / 对话导出图片。")
    if args.check:
        print("   补齐：python scripts/ops/fetch_static_libs.py")
    if network_errors:
        print(manual_hints(failed, target))
    else:
        print("   若文件是旧版本/被改动过，用 --force 重下；"
              "或删掉对应文件后重跑本脚本。")
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        raise SystemExit(130)
