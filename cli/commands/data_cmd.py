"""CLI 命令：/data — 全数据监控

用法：
  /data              概览（分类汇总 + TOP5 占用 + 上次扫描时间）
  /data scan         重新扫描并落快照
  /data tables <db>  某库表级明细
  /data trend        近 7 天趋势对比
  /data clean        dry-run 预览可清理项 → 确认后执行
  /data --json       任意子命令可加 --json 输出 JSON
"""
from __future__ import annotations

from cli.commands.base import CommandHandler, CommandResult


def _fmt_size(n: int) -> str:
    if n >= 1e9:
        return f'{n / 1e9:.2f} GB'
    if n >= 1e6:
        return f'{n / 1e6:.1f} MB'
    if n >= 1e3:
        return f'{n / 1e3:.0f} KB'
    return f'{n} B'


_dbm = None


def _get_dbm():
    """延迟加载 data_monitor 模块（测试可替换 _dbm）。"""
    global _dbm
    if _dbm is None:
        from utils import data_monitor
        _dbm = data_monitor
    return _dbm


class DataCommand(CommandHandler):
    name = "data"
    description = "全数据监控（大小/数量/趋势/清理）"
    usage = "/data [scan|tables|trend|clean] [--json]"

    async def execute(self, ctx, args: list[str], flags: dict | None = None) -> CommandResult:
        dbm = _get_dbm()

        flags = flags or {}
        fargs = [a for a in args if not a.startswith('-')]
        as_json = "--json" in args or flags.get('json')

        sub = fargs[0] if fargs else "overview"
        try:
            if sub == "scan":
                report = dbm.scan()
                return CommandResult(ok=True, message=self._render_overview(report), data=report)
            if sub == "tables":
                if len(fargs) < 2:
                    return CommandResult(ok=False, message="用法: /data tables <db名>")
                asset = self._find_asset(dbm, fargs[1])
                if not asset:
                    return CommandResult(ok=False, message=f"未找到资产: {fargs[1]}")
                return CommandResult(ok=True, message=self._render_tables(asset), data=asset)
            if sub == "trend":
                rows = dbm.trend(7)
                return CommandResult(ok=True, message=self._render_trend(rows), data=rows)
            if sub == "clean":
                return self._clean(dbm, as_json)
            rep = dbm.overview()
            return CommandResult(ok=True, message=self._render_overview(rep), data=rep)
        except Exception as e:  # noqa: BLE001
            return CommandResult(ok=False, message=f"数据监控失败: {e}")

    # ---------- 子命令实现 ----------

    def _clean(self, dbm, as_json: bool) -> CommandResult:
        preview = dbm.cleanup_preview()
        total = sum(p['size'] for p in preview)
        lines = [f"可清理项（{len(preview)} 类，预计释放 {_fmt_size(total)}）:"]
        for p in preview:
            lines.append(f"  [{p['id']}] {p['name']}: {p['count']} 项 / {_fmt_size(p['size'])}")
        pickable = [p for p in preview if p['count'] > 0]
        if not pickable:
            lines.append("当前没有可清理项。")
            return CommandResult(ok=True, message="\n".join(lines), data=preview)

        def _run():
            results = dbm.cleanup_run([p['id'] for p in pickable])
            freed = sum(r['freed_bytes'] for r in results.values())
            return CommandResult(
                ok=True,
                message=f"[OK] 已清理 {len(results)} 条规则，共释放 {_fmt_size(freed)}。",
                data=results,
            )

        if as_json:
            return CommandResult(ok=True, message="\n".join(lines), data=preview,
                                 requires_confirm=True, confirm_prompt="确认执行上述清理？输入 apply 执行 / 其他取消：",
                                 pending_action=_run)
        return CommandResult(ok=True, message="\n".join(lines), data=preview,
                             requires_confirm=True, confirm_prompt="确认执行上述清理？输入 apply 执行 / 其他取消：",
                             pending_action=_run)

    # ---------- 渲染 ----------

    @staticmethod
    def _find_asset(dbm, target: str):
        rep = dbm.overview()
        for a in rep.get('assets', []):
            if a['key'].endswith(target):
                return a
        return None

    @staticmethod
    def _render_overview(report: dict) -> str:
        from collections import defaultdict
        agg = defaultdict(lambda: {'size': 0, 'count': 0, 'n': 0})
        for a in report.get('assets', []):
            s = agg[a['category']]
            s['size'] += a['size_bytes'] or 0
            s['count'] += a['record_count'] or 0
            s['n'] += 1
        lines = [f"数据总览（扫描于 {report.get('scanned_at', '—')}，{len(report.get('assets', []))} 项资产）",
                 '─' * 48]
        for cat, s in sorted(agg.items(), key=lambda kv: -kv[1]['size']):
            lines.append(f"  {cat:<8} {_fmt_size(s['size']):>10}  {s['n']} 项  {s['count']:,} 记录")
        lines.append('─' * 48)
        top = sorted(report.get('assets', []), key=lambda a: -(a['size_bytes'] or 0))[:5]
        lines.append("TOP5 占用：")
        for a in top:
            lines.append(f"  {a['key']:<36} {_fmt_size(a['size_bytes'] or 0)}")
        return "\n".join(lines)

    @staticmethod
    def _render_tables(asset: dict) -> str:
        tables = asset.get('tables') or []
        if not tables:
            return f"{asset['key']} 无表级明细"
        lines = [f"{asset['key']} — {len(tables)} 张表", f"  {'表名':<28} {'记录数':>12} {'最新':<12} {'状态':<8}"]
        for t in tables:
            lines.append(f"  {t['name']:<28} {str(t['count']) if t['count'] is not None else '—':>12} "
                         f"{str(t.get('latest') or '—'):<12} {t['status']}")
        return "\n".join(lines)

    @staticmethod
    def _render_trend(rows: list) -> str:
        if not rows:
            return "暂无趋势数据，请先 /data scan"
        lines = ["近 7 天趋势（按天 × 分类）:"]
        for r in rows:
            lines.append(f"  {r['d']}  {r['category']:<8} {_fmt_size(r['size_bytes'] or 0):>10}  {r['record_count'] or 0:,}")
        return "\n".join(lines)