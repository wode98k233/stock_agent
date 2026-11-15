"""深度报告 Web 端到端验收（真浏览器 + 真后端任务）。

用法（先起服务）：
    $env:WEB_PORT='8031'; .\\venv\\Scripts\\python.exe -m server
    .\\venv\\Scripts\\python.exe scripts/devtools/e2e_deep_report_web.py --base http://127.0.0.1:8031 --sections 1

覆盖（对应评审缺陷 1–5）：
1) 填表 → 提交（URL 带 ?dialog_uuid=）→ 进度/取数事件 → 完成 → 阅读页；
2) 终态后 SSE 不再重连、iframe 不按 3s 反复重载（缺陷 1/2/3）；
3) 对话消息里落卡片哨兵，聊天页能渲染卡片，且终态后停止轮询（缺陷 4/5）；
4) 卡片 rid 非法不渲染；卡片被移出 DOM 后停止轮询；未知 rid 连续 404 后显示「状态未知」（缺陷 4）。
   注：Last-Event-ID 优先于 since_id 由 test/unit/server/test_deep_reports_api.py 覆盖（浏览器重连不好稳定模拟）。

退出码 0 = 通过；1 = 有失败项（打印明细）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit

try:
    from playwright.async_api import async_playwright
except Exception:  # pragma: no cover
    print("需要 playwright：pip install playwright && playwright install chromium", file=sys.stderr)
    raise SystemExit(2)

PAGE = "/static/deep-report.html"
CARD_RE = re.compile(r"@@DEEP_REPORT_START@@(.*?)@@DEEP_REPORT_END@@", re.S)


def _path(url: str) -> str:
    return urlsplit(url).path


def _snap_count(urls: list[str], rid: str) -> int:
    """卡片状态轮询次数（GET /api/deep-reports/<rid>，排除 /events 与 /html）。"""
    n = 0
    for u in urls:
        if f"/api/deep-reports/{rid}" not in u or "/events" in u:
            continue
        if _path(u).endswith("/html"):
            continue
        n += 1
    return n


def _is_expected_404(url: str) -> bool:
    # 阅读页在 finish 与 report.html 落盘之间有极小竞态；假卡片 rid 是主动制造 404
    return _path(url).endswith("/html") or "dr0000000000" in url or "dr1111111111" in url


async def run(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    bad: list[str] = []
    errs: list[str] = []
    dr_reqs: list[str] = []
    chat_reqs: list[str] = []
    results: list[tuple[str, bool, str]] = []
    log = ""
    frame_body = ""

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))
        print(f"[e2e] {'PASS' if ok else 'FAIL'} {name}{('：' + detail) if detail and not ok else ''}")

    def on_response(r):
        if r.status >= 400 and not _is_expected_404(r.url):
            bad.append(f"{r.status} {r.url}")

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1500, "height": 950})

        # 0) 准备一个对话（卡片要落在这个 dialog_uuid 里）
        dialog_uuid = args.dialog
        created_dialog = False
        if not dialog_uuid:
            resp = await ctx.request.post(f"{args.base}/api/dialogs",
                                          data={"title": "E2E 深度报告", "mode": "react_stock"})
            dialog_uuid = (await resp.json()).get("dialog_uuid") or ""
            created_dialog = True
        check("创建/复用对话", bool(dialog_uuid), f"dialog_uuid={dialog_uuid!r}")

        page = await ctx.new_page()
        page.on("request", lambda r: dr_reqs.append(r.url))
        page.on("response", on_response)
        page.on("pageerror", lambda e: errs.append(f"[deep-report] {e}"))

        await page.goto(f"{args.base}/static/deep-report.html", wait_until="networkidle")
        await page.wait_for_timeout(400)
        await page.screenshot(path=str(out / "01-initial.png"))

        # 1) 带 ?dialog_uuid= 打开生成页（等价于从对话页「深度报告」入口进入）
        await page.goto(f"{args.base}{PAGE}?dialog_uuid={quote(dialog_uuid)}",
                        wait_until="networkidle")
        hint = await page.inner_text("#dialogHint")
        check("页面给出「在对话中生成卡片」说明", "对话" in hint and "卡片" in hint, hint)
        check("生成页加载无 4xx", not bad, str(bad[:3]))
        bad.clear()

        await page.fill("#universe", args.universe)
        await page.fill("#req", args.request)
        await page.fill("#secs", str(args.sections))
        if args.fetch:
            await page.check("#fetch")
        await page.click("#start")
        await page.wait_for_function(
            "document.querySelector('#title').textContent.startsWith('dr')", timeout=30_000)
        rid = (await page.inner_text("#title")).strip()
        print(f"[e2e] rid={rid} dialog={dialog_uuid}")
        # 占位行现在由 service.start() 在 POST 返回前落库：创建瞬间不应再出现 /api/deep-reports/<rid> 404。
        await page.wait_for_timeout(1500)
        check("创建后无瞬时 404（占位行已提前落库）", not bad, str(bad[:3]))
        bad.clear()

        await page.wait_for_timeout(8000)
        await page.screenshot(path=str(out / "02-running.png"))

        # 2) 等终态；终态后 SSE 必须已关闭、iframe 不得反复重载
        await page.wait_for_function(
            "document.querySelector('#log').textContent.includes('完成：')", timeout=args.timeout * 1000)
        log = await page.inner_text("#log")
        check("SSE 收到 done 终态事件", "完成：" in log, log[:120])
        await page.wait_for_timeout(1500)
        ev_base = len([u for u in dr_reqs if "/events" in u])
        html_base = len([u for u in dr_reqs if _path(u).endswith("/html")])
        await page.wait_for_timeout(8000)
        ev_after = len([u for u in dr_reqs if "/events" in u])
        html_after = len([u for u in dr_reqs if _path(u).endswith("/html")])
        check("终态后 SSE 不再重连", ev_after == ev_base, f"{ev_base} -> {ev_after} 次 /events")
        check("终态后 iframe 不再重载", html_after == html_base, f"{html_base} -> {html_after} 次 /html")
        await page.screenshot(path=str(out / "03-done.png"))

        # 3) 刷新后仍在 / 能打开阅读页（原有能力回归）
        await page.reload(wait_until="networkidle")
        await page.wait_for_timeout(1200)
        items = await page.locator(".item").count()
        await page.screenshot(path=str(out / "04-reload.png"))
        check("刷新后列表仍能列出报告", items >= 1, f"{items} 项")
        await page.click(f'[data-rid="{rid}"]')
        await page.wait_for_timeout(2500)
        await page.screenshot(path=str(out / "05-reader.png"))
        frame_body = (await page.frame_locator("#frame").locator("body").inner_text())[:120]
        check("阅读页能加载正文", "目录" in frame_body, frame_body)

        # 4) 对话里必须落了卡片哨兵（rid 一致）
        resp = await ctx.request.get(f"{args.base}/api/dialogs/{dialog_uuid}/messages")
        msgs = (await resp.json()).get("items") or []
        card_payloads = []
        for m in msgs:
            mm = CARD_RE.search(m.get("content") or "")
            if mm:
                try:
                    card_payloads.append(json.loads(mm.group(1)))
                except Exception:  # noqa: BLE001
                    pass
        check("对话消息里有卡片哨兵", any(c.get("rid") == rid for c in card_payloads),
              f"messages={len(msgs)} cards={card_payloads}")

        # 5) 聊天页：卡片能渲染、入口带当前对话、终态后停止轮询
        chat = await ctx.new_page()
        chat.on("request", lambda r: chat_reqs.append(r.url))
        chat.on("response", on_response)
        chat.on("pageerror", lambda e: errs.append(f"[chat] {e}"))
        await chat.goto(f"{args.base}/", wait_until="networkidle")
        try:
            await chat.wait_for_selector(f'.dialog-item[data-uuid="{dialog_uuid}"]', timeout=20_000)
            await chat.click(f'.dialog-item[data-uuid="{dialog_uuid}"]')
            await chat.wait_for_selector(".dr-chat-card", timeout=20_000)
        except Exception as e:  # noqa: BLE001
            check("聊天页渲染卡片", False, str(e)[:120])
        else:
            await chat.wait_for_function(
                """() => {
                  const b = document.querySelector('.dr-chat-card .dr-badge');
                  return !!b && (b.textContent.includes('已完成') || b.textContent.includes('部分完成'));
                }""", timeout=20_000)
            check("聊天页渲染卡片并进入终态", True)
            await chat.screenshot(path=str(out / "06-chat-card.png"))

            href = await chat.get_attribute("#deepReportBtn", "href") or ""
            check("sidebar 深度报告入口带当前 dialog_uuid",
                  f"dialog_uuid={dialog_uuid}" in href, href)

            await chat.wait_for_timeout(1000)
            snap_base = _snap_count(chat_reqs, rid)
            await chat.wait_for_timeout(6500)
            snap_after = _snap_count(chat_reqs, rid)
            check("卡片终态后停止轮询", snap_after == snap_base, f"{snap_base} -> {snap_after} 次")

            # 5.1) rid 非法 → 不渲染
            invalid = await chat.evaluate(
                "() => [window.StockRadar.deepReportCard.render({}),"
                " window.StockRadar.deepReportCard.render({rid: '../x'})]")
            check("卡片 rid 缺失/非法不渲染", all(x is None for x in invalid), str(invalid))

            # 5.2) 卡片被移出 DOM → MutationObserver 清理定时器
            await chat.evaluate(
                """() => {
                  const el = window.StockRadar.deepReportCard.render({rid: 'dr0000000000'});
                  if (!el) throw new Error('valid rid render returned null');
                  window.__drFake = el;
                  document.getElementById('messageList').appendChild(el);
                }""")
            await chat.wait_for_timeout(3500)
            fake_base = _snap_count(chat_reqs, "dr0000000000")
            await chat.evaluate("() => window.__drFake.remove()")
            await chat.wait_for_timeout(5000)
            fake_after = _snap_count(chat_reqs, "dr0000000000")
            check("卡片移出 DOM 后停止轮询", fake_after == fake_base, f"{fake_base} -> {fake_after} 次")

            # 5.3) 未知 rid：连续 404（5 次 × 3s）后停止并显示「状态未知」
            await chat.evaluate(
                """() => {
                  const el = window.StockRadar.deepReportCard.render({rid: 'dr1111111111'});
                  window.__drFake404 = el;
                  document.getElementById('messageList').appendChild(el);
                }""")
            try:
                await chat.wait_for_function(
                    """() => [...document.querySelectorAll('.dr-chat-card')].some(el =>
                      el.dataset.drRid === 'dr1111111111' && el.textContent.includes('状态未知'))""",
                    timeout=25_000)
                stopped = True
            except Exception:  # noqa: BLE001
                stopped = False
            check("未知 rid 连续 404 后停止并提示", stopped)
            await chat.screenshot(path=str(out / "07-card-404.png"))
            await chat.evaluate("() => window.__drFake404.remove()")

            # 5.4) 日历「深度报告」列可见（trading-calendar.js 必须升到 ?v=4 才能拿到新代码）
            cal_src = await chat.evaluate(
                """() => {
                  const s = [...document.querySelectorAll('script[src]')]
                    .find(x => x.src.includes('trading-calendar.js'));
                  return s ? s.src : '';
                }""")
            check("日历脚本版本号生效（?v=4，绕开 immutable 缓存）",
                  "trading-calendar.js?v=4" in cal_src, cal_src)
            try:
                await chat.click('button[data-menu="menuQuote"]')
                await chat.click("#calendarBtn")
                await chat.wait_for_selector(".calendar-modal.open", timeout=20_000)
                await chat.wait_for_function(
                    """() => [...document.querySelectorAll('#calendarBody .calendar-record-mode')]
                      .some(el => el.textContent.trim() === '深度报告')""",
                    timeout=25_000)
                cal_ok = True
            except Exception as e:  # noqa: BLE001
                cal_ok = False
                print(f"[e2e] 日历断言异常：{str(e)[:160]}")
            check("日历当天出现「深度报告」列", cal_ok)
            await chat.screenshot(path=str(out / "08-calendar.png"))

        if created_dialog and not args.keep:
            await ctx.request.delete(f"{args.base}/api/dialogs/{dialog_uuid}")

        await browser.close()

    for item in bad:
        errs.append(f"HTTP {item}")

    ok = all(ok for _, ok, _ in results) and not errs
    if errs:
        print("[FAIL] 页面/网络错误：", errs[:8])
    print(f"[e2e] log: {log.replace(chr(10), ' | ')[:300]}")
    print(f"[e2e] 阅读页正文片段: {frame_body.replace(chr(10), ' ')[:80]}")
    print(f"[e2e] {sum(1 for _, ok_, _ in results if ok_)}/{len(results)} 项断言通过")
    print("[e2e] PASS" if ok else "[e2e] FAIL")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8011")
    ap.add_argument("--universe", default="银行板块")
    ap.add_argument("--request", default="现在还能追高吗，要估值和资金面")
    ap.add_argument("--sections", type=int, default=2)
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--timeout", type=int, default=420, help="等待完成的秒数")
    ap.add_argument("--out", default="output/e2e_deep_report")
    ap.add_argument("--dialog", default="", help="复用已有对话 uuid（默认新建并在结束后删除）")
    ap.add_argument("--keep", action="store_true", help="保留本次新建的对话")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
