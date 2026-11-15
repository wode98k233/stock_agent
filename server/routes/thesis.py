"""研究台账 API：/api/thesis/*

编排走 `server.thesis_service`（run_in_executor，不阻塞事件循环）。
错误一律转成 `{code:1, msg:...}` 而不是抛 500——台账页白屏比报错更难排查，
且「取数失败」本来就该以降级形状呈现（因子标数据缺失）而不是整体失败。
"""
import asyncio
import logging

from fastapi import APIRouter, Query

logger = logging.getLogger("radar.thesis")

router = APIRouter(prefix="/api/thesis", tags=["thesis"])


def _svc():
    from server import thesis_service
    return thesis_service


async def _run(fn):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fn)


def _ok(data):
    return {"code": 0, "data": data, "msg": "ok"}


def _err(msg: str):
    return {"code": 1, "data": None, "msg": msg}


# ── 列表（须在 /{thesis_id} 之前声明，否则被路径参数吞掉） ────────────

@router.get("/list")
async def list_theses():
    """台账列表：定义目录 × 最新快照状态（从未刷新过也在列）。"""
    try:
        items = await _run(lambda: _svc().list_theses())
        return _ok({"items": items, "count": len(items)})
    except Exception as e:
        logger.warning("[thesis] list 失败: %s", e)
        return _err(f"台账列表读取失败: {e}")


@router.get("/alerts")
async def all_alerts(days: int = Query(30, ge=1, le=365)):
    """全部台账的变更告警（跨台账视图）。"""
    try:
        items = await _run(lambda: _svc().get_alerts(None, days))
        return _ok({"items": items, "count": len(items)})
    except Exception as e:
        logger.warning("[thesis] alerts 失败: %s", e)
        return _err(f"告警读取失败: {e}")


@router.get("/portfolio")
async def portfolio():
    """组合层建议：把驱动腿台账的总分映射成哑铃组合的建议权重区间。

    与 /list、/alerts 同理，**必须声明在 /{thesis_id} 之前**，否则被路径参数吞掉
    （表现为请求 /api/thesis/portfolio 返回「台账定义不存在: portfolio」）。
    """
    try:
        return _ok(await _run(lambda: _svc().get_portfolio_suggestion()))
    except Exception as e:
        logger.warning("[thesis] portfolio 失败: %s", e)
        return _err(f"组合建议读取失败: {e}")


@router.get("/portfolios")
async def portfolios():
    """组合列表（多组合视图）：每个组合的当前建议权重区间 + 共同风险。

    同 /list、/alerts、/portfolio，**必须声明在 /{thesis_id} 之前**。
    """
    try:
        items = await _run(lambda: _svc().list_portfolios())
        return _ok({"items": items, "count": len(items)})
    except Exception as e:
        logger.warning("[thesis] portfolios 失败: %s", e)
        return _err(f"组合列表读取失败: {e}")


@router.get("/market-matrix")
async def market_matrix():
    """资产 × 时间窗口涨跌幅热力图 + 相关性矩阵（本地日线自算）。

    同 /list、/alerts、/portfolio，**必须声明在 /{thesis_id} 之前**。
    """
    try:
        return _ok(await _run(lambda: _svc().get_market_matrix()))
    except Exception as e:
        logger.warning("[thesis] market-matrix 失败: %s", e)
        return _err(f"市场矩阵读取失败: {e}")


@router.get("/{thesis_id}/report")
async def report(thesis_id: str, force: int = Query(0, ge=0, le=1)):
    """报告数据一次性组装（契约 5.4）：详情 + 历史 + 证据(details=1) + 归因 + 告警。

    刻意声明在 `/{thesis_id}` 之前：与 /list、/alerts 同理，宁可让静态段先匹配；
    证据计算失败时服务层降级成 `evidence.ok=false`，报告仍会返回（缺证据区）。

    默认复用 30 分钟内的报告数据（`cache.hit=true`）；`?force=1` 强制重算
    （前端「重新生成」按钮用），`refresh` 会主动失效。
    """
    try:
        if force:
            fn = lambda: _svc().get_report_data(thesis_id, force=True)
        else:
            fn = lambda: _svc().get_report_data(thesis_id)
        return _ok(await _run(fn))
    except ValueError as e:                    # 定义不存在是配置问题，必须明确报出
        logger.warning("[thesis] report(%s) 定义问题: %s", thesis_id, e)
        return _err(str(e))
    except Exception as e:
        logger.warning("[thesis] report(%s) 失败: %s", thesis_id, e)
        return _err(f"台账报告数据组装失败: {e}")


@router.get("/{thesis_id}/chart")
async def chart(thesis_id: str,
                force: int = Query(0, ge=0, le=1),
                points: int = Query(1200, ge=50, le=5000)):
    """双轴复盘图：等权总分 / 翻转后总分 vs 标的价格（左右双轴）+ 未来 h 期价格。

    由 `thesis_dual_axis.py` 的一次性脚本产品化而来：因子按当前定义**重放**历史
    （无前视），覆盖率面板保证 >=100 期；`fut[i]=price[i+h]` 尾部 h 个 null。

    缓存是三级（细节见 `docs/areas/thesis.md`）：
    - L1 进程内存 key=`(thesis_id, def_hash, h)`、TTL 1h（**不含数据日**：版本没变
      1 小时内直接复用，即使数据前进一天；到期后探测数据日，没变只续期不重算）；
    - L2 持久 `cache_kv` key=`cache_thesis_chart:{tid}:{def_hash}:{h}`、TTL 1h，跨进程
      （后台预热脚本写、web server 读）；
    - L3 因子级 `cache_thesis_factor:*`、TTL 24h，输入轴没前进就不 replay。
    `?force=1` 跳过 L1/L2 强制重算（仍复用 L3）；`refresh` 只在定义/数据真的变了时清该台账 L1/L2。
    冷算会触发引擎补数（实测 5~30s，慢的台账 40s+），热缓存 <100ms；总览页请由
    前端按台账并发调用单张接口，**不要**在请求里串行算 8 张。
    """
    try:
        fn = lambda: _svc().get_chart_data(
            thesis_id, force=bool(force), points=points)
        return _ok(await _run(fn))
    except ValueError as e:                    # 定义/目标不可用是配置问题，明确报出
        logger.warning("[thesis] chart(%s) 定义问题: %s", thesis_id, e)
        return _err(str(e))
    except Exception as e:
        logger.warning("[thesis] chart(%s) 失败: %s", thesis_id, e)
        return _err(f"双轴图数据生成失败: {e}")


@router.get("/{thesis_id}")
async def detail(thesis_id: str):
    """台账详情：最新快照 + 定义元信息 + 近期告警 + 定义变更历史。"""
    try:
        data = await _run(lambda: _svc().get_detail(thesis_id))
        if not data.get("definition"):
            return _err(f"台账定义不存在: {thesis_id}")
        return _ok(data)
    except Exception as e:
        logger.warning("[thesis] detail(%s) 失败: %s", thesis_id, e)
        return _err(f"台账详情读取失败: {e}")


@router.post("/{thesis_id}/refresh")
async def refresh(thesis_id: str):
    """重算并落快照：返回新快照与本次产生的变更告警。"""
    try:
        data = await _run(lambda: _svc().refresh(thesis_id))
        return _ok(data)
    except ValueError as e:
        logger.warning("[thesis] refresh(%s) 定义问题: %s", thesis_id, e)
        return _err(str(e))
    except Exception as e:
        logger.warning("[thesis] refresh(%s) 失败: %s", thesis_id, e)
        return _err(f"台账刷新失败: {e}")


@router.get("/{thesis_id}/history")
async def history(thesis_id: str, days: int = Query(90, ge=1, le=1095)):
    """趋势历史（升序），供页面上画总分曲线与因子评分条带。"""
    try:
        return _ok(await _run(lambda: _svc().get_history(thesis_id, days)))
    except Exception as e:
        logger.warning("[thesis] history(%s) 失败: %s", thesis_id, e)
        return _err(f"台账历史读取失败: {e}")


@router.get("/{thesis_id}/evidence")
async def factor_evidence(thesis_id: str, factor: str = Query(...)):
    """单因子关联性证据：因子历史序列 vs 标的未来 20/60 日收益（散点 + 分层）。

    复合因子（双轴矩阵/相对强弱/手工项）返回 supported=False 与原因——不做假图。
    """
    try:
        return _ok(await _run(lambda: _svc().get_factor_evidence(thesis_id, factor)))
    except Exception as e:
        logger.warning("[thesis] evidence(%s,%s) 失败: %s", thesis_id, factor, e)
        return _err(f"因子证据读取失败: {e}")


@router.get("/{thesis_id}/factor-evidence")
async def all_factor_evidence(thesis_id: str, details: int = Query(0, ge=0, le=1)):
    """**全因子**有效性汇总（因子有效性回测的产品化入口）。

    与单因子 `/evidence` 的区别：一次跑完整张台账，输出 强/中/弱/不可测 的分档
    分布 + 逐因子明细，用于「这张台账的因子有几个真有区分度」的准入检查。
    ctx 只准备一次，故比逐因子串行快得多。

    `details=1` 额外带滚动相关曲线与分层明细（报告图片用，体积明显更大）。
    """
    try:
        return _ok(await _run(
            lambda: _svc().get_all_factor_evidence(thesis_id, details=bool(details))))
    except Exception as e:
        logger.warning("[thesis] factor-evidence(%s) 失败: %s", thesis_id, e)
        return _err(f"全因子证据读取失败: {e}")


@router.get("/{thesis_id}/alerts")
async def thesis_alerts(thesis_id: str, days: int = Query(30, ge=1, le=365)):
    """单个台账的变更告警。"""
    try:
        items = await _run(lambda: _svc().get_alerts(thesis_id, days))
        return _ok({"items": items, "count": len(items)})
    except Exception as e:
        logger.warning("[thesis] alerts(%s) 失败: %s", thesis_id, e)
        return _err(f"告警读取失败: {e}")
