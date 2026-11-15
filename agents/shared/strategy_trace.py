"""策略回测的**独立执行上下文**（C 批：把策略从 plan/react 图里彻底拆出来）。

## 为什么要有这个文件

用户的原话：
> 「策略这个可以单独搞个 agent，没必要贴合我们这些 agent 模式了。什么 plan react
> 都是为了研报做准备的，和这个策略，确实不搭配。」

之前策略回测是「贴在图上的一段」：每一轮用户输入先被
`agents/shared.strategy_gate.py` 拦下，命中就整个绕开 agent 图，由
`strategy_session_runner.run_session_turn` 接管。这解决了「别被研报模式截胡」，
但没解决三件事：

1. **研发期 8 轮 LLM 调用完全不被统计**。trace 的 run 是 LangGraph 建的
   （`TraceRecorder.start_run` 由图执行器触发），策略轮压根不进图 → 没有 run →
   月历/token 统计里这段消耗凭空消失，而它恰恰是最贵的一段（8 轮生成 + 变异）。
2. **trace run 归属靠猜**。`server/agent_runner.py` 只能兜底取
   `find_latest_trace_run_id()`，分库跨月回退时会命中别的对话的陈旧 running 行，
   于是「执行过程」显示成上周的 43 节点周报，token 也被错记到本对话名下。
   代码里已经为此留了空位（`_is_strategy_turn` 分支 trace_run_id 留空），等的就是这里。
3. **无法独立演进**。策略的槽位、口径、迭代史跟研报的图节点耦合在一起，
   后面加「多标的组合」「参数寻优」这类策略专属能力时没有干净的落点。

## 本文件的职责边界（刻意很窄）

只做三件事，不碰业务逻辑：

- 自建一个 trace run，并把 run_id **显式绑定**回会话/任务，取代「猜最近 run」；
- 在 run 内为研发期的每一轮（生成 / 编译 / 回测 / 评估 / 决策）打 step，
  让 8 轮 LLM 调用与 IS/OOS 双段回测在执行过程里看得见；
- 独立 SSE 事件出口，页面据此渲染进度条，不依赖 agent 图的 progress 协议。

⛔ 不做的事：不复制业务逻辑、不重新实现策略生成/回测、不改 DB schema。
真正的解耦（独立 HTTP 入口、独立前端页）是下一步，本文件先把「可观测 + 可归属」补上——
这是解耦的前置条件：没有独立 run，拆出去也只是换个地方跑而已。

## 用法

    from agents.shared.strategy_trace import strategy_turn_scope
    with strategy_turn_scope(session, user_input) as scope:
        ...执行一轮...
        scope.record_iter(round_no=1, ...)   # 可选：研发迭代打点

研发后台线程那边用 `strategy_research_scope`，它把 8 轮迭代逐步写进同一个 run。
两者可以嵌套（会话轮创建 run → 起跑后由研发线程续写），run_id 通过
``strategy_run_scope`` 这个 ContextVar 传递。
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

# 当前策略执行上下文正在使用的 trace run_id。
# 由 strategy_turn_scope / strategy_research_scope 写入，由后台线程读取 ——
# 这样「起跑」这个动作发生在会话轮里，而研发循环跑在另一个线程，两者仍属同一个 run。
strategy_run_scope: ContextVar[Optional[str]] = ContextVar(
    'strategy_run_scope', default=None)

# 研发循环的迭代打点回调：(round_no, phase, payload) -> None
# 由 ResearchScope 注入到 StrategyResearchLoop.progress_cb 链路上。
_strategy_iter_hook: ContextVar[Optional[Any]] = ContextVar(
    '_strategy_iter_hook', default=None)


# ============================================================
# recorder 生命周期
# ============================================================

@contextmanager
def _owned_recorder(agent_name: str, task_input: Any) -> Iterator[Any]:
    """建一个 TraceRecorder、注册进 ContextVar、退出时确保收尾。

    ``_active_recorder`` 是 LangChain 的全局 configure hook 上下文变量：
    把它设好之后，本上下文里所有 ChatModel/LLM 调用都会自动被埋点，
    不需要逐个调用点改代码 —— 这是「零侵入 trace」的关键，必须成对 set/reset。

    ⚠️ 异常路径也要 end_run：否则 run 永远停在 running，页面「执行过程」会一直转圈，
    而且下次统计「最近 run」时还会命中这条陈旧行（分库跨月回退尤其致命）。
    """
    from utils.agent_trace.core import TraceRecorder, _active_recorder

    recorder = TraceRecorder(agent_name=agent_name)
    token = _active_recorder.set(recorder)
    run_id = ''
    try:
        run_id = recorder.start_run(task_input)
        yield recorder
    except Exception as exc:
        # 先 yield 出去让业务跑，再在这里兜底收尾
        if recorder._root:
            try:
                recorder.end_run(status='error', error=str(exc))
            except Exception:
                pass
        raise
    finally:
        if recorder._root and getattr(recorder, '_run_created', False):
            # 正常路径若业务没显式 end_run，这里补一次（幂等：end_run 内部按 _root 判定）
            row = None
            try:
                conn = recorder._get_conn()
                row = conn.execute(
                    'SELECT status FROM runs WHERE id=?', (recorder._root,)
                ).fetchone()
            except Exception:
                pass
            if row is not None and str(row[0]) == 'running':
                try:
                    recorder.end_run()
                except Exception:
                    pass
        try:
            _active_recorder.reset(token)
        except Exception:
            pass


@contextmanager
def _attached_recorder(agent_name: str, run_id: str) -> Iterator[Any]:
    """把 recorder **接**到一条已存在的 run 上（不新建 runs 行、不负责 end_run）。

    用于「同一条 run 被多个上下文续写」：会话续轮、研发后台线程。
    _active_recorder 是 LangChain 的 configure hook ContextVar，只在设置它的
    那个上下文里生效 —— 跨轮 / 跨线程必须重新挂一次，否则这一段的 LLM 调用
    与 step 全部丢失（月历里就表现为「有 run、token 恒 0」）。
    """
    from utils.agent_trace.core import TraceRecorder, _active_recorder

    recorder = TraceRecorder(agent_name=agent_name)
    recorder.attach_run(run_id)
    token = _active_recorder.set(recorder)
    try:
        yield recorder
    finally:
        try:
            _active_recorder.reset(token)
        except Exception:
            pass


# ============================================================
# 会话轮：用户对话这一轮
# ============================================================

@contextmanager
def strategy_turn_scope(session: Dict[str, Any], user_input: str,
                        agent_name: str = 'strategy_session') -> Iterator['TurnScope']:
    """一轮策略对话的独立执行上下文（自建 trace run + 显式绑定）。

    产出的 run_id 会写回 ``session['trace_run_id']``（内存字典，调用方负责落库），
    供 ``server/agent_runner.py`` 直接取用 —— 这就是「显式绑定」取代
    ``find_latest_trace_run_id()`` 猜最近 run 的落点。
    """
    existing = str((session or {}).get('trace_run_id') or '')
    if existing:
        # 同一会话的续轮：沿用首个 run，避免一个会话被切成 20 个 run。
        # ⚠️ 必须重新挂一个 attached recorder：_active_recorder 是 ContextVar，
        # 上一轮的 recorder 早已随作用域退出 —— 不挂的话续轮的 LLM 调用与
        # step 全都写不进去（实测：63 条会话 run 全部 0 step）。
        with _attached_recorder(agent_name, existing) as recorder:
            scope = TurnScope(run_id=existing, recorder=recorder, created=False)
            token = strategy_run_scope.set(existing)
            try:
                yield scope
            finally:
                strategy_run_scope.reset(token)
        return

    session_uuid = str((session or {}).get('session_uuid') or '')
    task_input = {'session_uuid': session_uuid,
                  'input': (user_input or '')[:200],
                  'idea': str((session or {}).get('idea') or '')[:200]}
    with _owned_recorder(agent_name, task_input) as recorder:
        run_id = str(getattr(recorder, '_root', '') or '')
        scope = TurnScope(run_id=run_id, recorder=recorder, created=True)
        token = strategy_run_scope.set(run_id)
        if run_id and session is not None:
            # 显式绑定：agent_runner 直接读这个字段，不再去 DB 里猜。
            # ⚠️ 必须写在 yield **之前**：作用域内的后续代码要靠它复用这条 run ——
            # 闸门建完会话再委派 run_session_turn 时，runner 见到 trace_run_id
            # 会走「沿用」分支，否则同一轮会建出两条 run。
            session['trace_run_id'] = run_id
            logger.info('[策略会话] trace run 已绑定: session=%s run=%s',
                        session_uuid, run_id)
        try:
            yield scope
        finally:
            strategy_run_scope.reset(token)


class TurnScope:
    """一轮会话的 trace 句柄。方法都是「有 recorder 才生效」，无 recorder 时静默降级。"""

    def __init__(self, run_id: str, recorder: Any = None, created: bool = False):
        self.run_id = run_id or ''
        self.recorder = recorder
        self.created = created

    def step(self, name: str, input_data: Any = None, extra: Optional[dict] = None):
        """在该 run 下开一个 step（用作 with，退出即finish）。"""
        if not self.recorder or not self.run_id:
            return _NullStep()
        return _RecorderStep(self.recorder, self.run_id, name, input_data, extra)


class _NullStep:
    """无 recorder 时的空 step 句柄（trace 不可用不该打断业务）。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def set_output(self, output: Any) -> None:
        """与 _RecorderStep 同形：无 trace 时静默丢弃。"""
        return None


class _RecorderStep:
    def __init__(self, recorder: Any, run_id: str, name: str,
                 input_data: Any, extra: Optional[dict]):
        self._recorder = recorder
        self._run_id = run_id
        self._name = name
        self._input = input_data
        self._extra = dict(extra or {}, label=name)
        self._opened = False

    def __enter__(self):
        try:
            self._recorder._create_step(
                self._run_id, None, 'strategy', self._name,
                self._input, self._extra)
            self._opened = True
        except Exception:
            logger.debug('[策略会话] step 创建失败（忽略）', exc_info=True)
        return self

    def set_output(self, output: Any) -> None:
        if self._opened:
            try:
                self._recorder._finish_step(self._run_id, output=output)
                self._opened = False
            except Exception:
                pass

    def __exit__(self, exc_type, exc, tb):
        if not self._opened:
            return False
        try:
            if exc_type is not None:
                self._recorder._finish_step(
                    self._run_id, status='error', error=str(exc))
            else:
                self._recorder._finish_step(self._run_id)
        except Exception:
            pass
        return False


# ============================================================
# 研发线程：8 轮迭代
# ============================================================

@contextmanager
def strategy_research_scope(run_uuid: str, idea: str = '',
                           agent_name: str = 'strategy_research',
                           bind: Optional[Dict[str, Any]] = None) -> Iterator['ResearchScope']:
    """研发后台线程的独立执行上下文。

    与 ``strategy_turn_scope`` 的区别：run 由**本作用域自己建**（后台线程拿不到会话轮的
    recorder —— 线程 + ContextVar 不共享），但如果会话轮已经建过 run（``bind`` 里带了
    ``trace_run_id``），就沿用它，保证「起跑」这个动作和 8 轮迭代属于同一条执行记录。

    研发循环在 ``StrategyResearchLoop`` 里通过 :func:`current_iter_hook` 拿到打点回调，
    逐轮写入 GEN / COMPILE / BACKTEST / EVAL / DECIDE 五个 phase。
    """
    inherited = str((bind or {}).get('trace_run_id') or strategy_run_scope.get() or '')
    if inherited:
        # 会话轮已经建过 run：研发线程「接上去」而不是另建一条 —— 用户视角
        # 「起跑」与 8 轮迭代是同一次任务，过程流 / 月历都该是一条记录。
        # ⚠️ 必须挂 recorder，否则 record_iter 与 8 轮 LLM 调用（token 大户）
        # 一条都不落库（实测：63 条 run 里 steps 恒为 0）。
        with _attached_recorder(agent_name, inherited) as recorder:
            scope = ResearchScope(run_id=inherited, recorder=recorder, created=False)
            token = strategy_run_scope.set(inherited)
            hook_token = _strategy_iter_hook.set(scope.record_iter)
            try:
                yield scope
            finally:
                _strategy_iter_hook.reset(hook_token)
                strategy_run_scope.reset(token)
        return

    task_input = {'run_uuid': run_uuid, 'idea': (idea or '')[:200]}
    with _owned_recorder(agent_name, task_input) as recorder:
        run_id = str(getattr(recorder, '_root', '') or '')
        scope = ResearchScope(run_id=run_id, recorder=recorder, created=True)
        token = strategy_run_scope.set(run_id)
        hook_token = _strategy_iter_hook.set(scope.record_iter)
        try:
            yield scope
        finally:
            _strategy_iter_hook.reset(hook_token)
            strategy_run_scope.reset(token)
            if run_id and isinstance(bind, dict):
                # 回传给调用方落库（strategy_run.trace_run_id）
                bind['trace_run_id'] = run_id
            logger.info('[策略研发] trace run: run_uuid=%s trace_run=%s',
                        run_uuid, run_id)


class ResearchScope:
    """研发 run 的 trace 句柄：逐轮迭代打点。"""

    def __init__(self, run_id: str, recorder: Any = None, created: bool = False):
        self.run_id = run_id or ''
        self.recorder = recorder
        self.created = created
        self._round_t0: Dict[int, float] = {}

    def record_iter(self, round_no: int, phase: str, payload: Optional[dict] = None):
        """记录一个迭代 phase。phase ∈ gen/compile/backtest/eval/decide/terminate。

        直接写 steps 表而不走 LLM callback —— 研发循环的 LLM 调用走
        ``utils.llm.gateway``，不一定经过 LangChain callback 体系，手写 step
        才能保证「每轮都能看到」而不是「偶尔记到」。
        """
        if not self.recorder or not self.run_id:
            return
        try:
            now = time.perf_counter()
            key = int(round_no)
            if key <= 0:
                # 轮号<=0 = 整体级事件（如 run_done），不写「第0轮」污染展示
                key = 0
            if phase == 'gen' and key:
                self._round_t0[key] = now
            prev = self._round_t0.get(key)
            label = ('第%d轮-%s' % (key, phase)) if key else ('整体-%s' % phase)
            extra = dict(payload or {}, label=label, phase=phase, round_no=key)
            self.recorder._create_step(
                self.run_id, None, 'strategy', label,
                {'round_no': key, 'phase': phase}, extra)
            duration = None
            if phase == 'decide' and prev is not None:
                duration = round((now - prev) * 1000, 2)
            if duration is not None:
                self.recorder._finish_step(
                    self.run_id, output=dict(payload or {}))
            else:
                # 非轮末 phase：立刻结束，耗时为 0（真实耗时在 decide 里体现）
                self.recorder._finish_step(self.run_id, output=dict(payload or {}))
        except Exception:
            logger.debug('[策略研发] 迭代埋点失败（忽略）', exc_info=True)


def current_iter_hook():
    """供 ``StrategyResearchLoop`` 取当前上下文的迭代打点回调（无则 None）。"""
    return _strategy_iter_hook.get()


def current_run_id() -> str:
    """当前策略执行上下文的 trace run_id（无则空串）。

    ⛔ 禁止用「最近一条 running run」代替它 —— 分库跨月回退会命中文档性陈旧残留行，
    过程流会显示上一次甚至上个月任务的执行过程（见模块头注释）。
    """
    return str(strategy_run_scope.get() or '')


# 闸门拦截完成后，本轮策略执行实际用到的 trace run_id。
# 为什么不用返回值透传：``intercept_strategy_turn`` 的返回契约是「str 或 None」，
# 改成返回元组会牵动 agent_runner 与全部既有单测；而 trace 归属是**旁路信息**，
# 用 ContextVar 透传既不污染主契约，也天然带作用域（会话轮结束即失效）。
last_turn_run_scope: ContextVar[str] = ContextVar('last_turn_run_scope', default='')


def publish_turn_run(run_id: str) -> None:
    """闸门侧调用：把本轮策略执行用到的 run_id 发布出去。"""
    last_turn_run_scope.set(str(run_id or ''))