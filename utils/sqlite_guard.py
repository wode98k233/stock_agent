"""SQLite 连接守卫 —— 挡住「工厂属性被置空 → 下次查询原生崩溃」这条坑。

背景（2026-09-21 崩溃排查）
--------------------------
CPython 3.11.1 的 `_sqlite3` 存在一个直到 **3.16 才修掉**的官方缺陷：
`del conn.row_factory` / `del conn.text_factory` 会把 C 层槽位置成 NULL，
而查询路径仍然无条件把该槽位当可调用对象去调 → `Py_TYPE(NULL)` →
访问违例（`0xC0000005`，故障模块 `python311.dll`，目标地址恒为 `0x8`）→
**进程直接消失，Python 层连异常都没有**。

实测结论（2026-09-21 现场验证，脚本为一次性、已清理）：

- `del conn.row_factory` 后，Python 侧读 `conn.row_factory` **仍返回 `None`**，
  与健康连接的默认值完全无法区分 → 「启动期断言槽位非空」这条路走不通；
- `del` 也不产生任何 audit 事件 → `sys.addaudithook` 拦不住。

因此唯一能在 Python 层生效的手段是：**让连接是 Python 子类，在
`__delattr__` 里直接拒绝这次删除**（实测可行：`sqlite3.Connection`
是 `Py_TPFLAGS_BASETYPE`，可以被继承、可以走 `connect(factory=...)`）。

定位说明
--------
本模块防的是「`del` 工厂属性」这个**已确认可复现**的坑，属纵深防御；
2026-09-21 那次线上崩溃**尚未定位到具体是谁置空的**（项目内无 `del`），
所以它不等于根因修复 —— 根因仍需 `logs/native_crash.log` 里的现场栈。
"""
from __future__ import annotations

import sqlite3
from typing import Optional

# C 层槽位被置空后查询必崩的两个属性（其余属性删除无害）
FACTORY_ATTRS = ("row_factory", "text_factory")


class GuardedConnection(sqlite3.Connection):
    """禁止删除 `row_factory` / `text_factory` 的 sqlite3 连接子类。

    健康连接里 `row_factory` 的默认值就是 `None`（C 层是 `Py_None`），
    与「槽位被置成 NULL」在 Python 侧同形；所以只能禁止删除，无法事后检测。
    """

    def __delattr__(self, name: str) -> None:  # pragma: no cover - 触发即异常
        if name in FACTORY_ATTRS:
            raise AttributeError(
                f"禁止删除 sqlite3.Connection.{name}："
                "该槽位被置空后，下一次查询会以 NULL 为可调用对象调用，"
                "触发解释器原生崩溃（0xC0000005，无法被 try/except 捕获）。"
                "如确实要恢复默认行为，请显式赋值（row_factory=None / text_factory=str）。"
            )
        super().__delattr__(name)


def connect(
    database: str,
    *,
    factory: Optional[type] = None,
    row_factory: Optional[object] = None,
    **kwargs,
) -> sqlite3.Connection:
    """`sqlite3.connect` 的守卫版：默认返回 `GuardedConnection`。

    参数与 `sqlite3.connect` 一致（`timeout` / `check_same_thread` / `uri` 等
    通过 `**kwargs` 透传）。`factory` 仅在调用方有特殊需求时才传；不传即受守护。
    """
    conn = sqlite3.connect(database, factory=factory or GuardedConnection, **kwargs)
    if row_factory is not None:
        conn.row_factory = row_factory
    return conn
