"""
Reranker 微服务 — 独立进程
===========================
运行 BAAI/bge-reranker-v2-m3（sentence_transformers.CrossEncoder），
对外暴露 OpenAI 兼容的 ``/v1/rerank`` 接口。

从主进程剥离，主进程不再常驻 ~700MB-1GB 权重。

主程序侧配置（零代码改动）：
    STOCK_MEMORY_RERANKER=openai
    STOCK_MEMORY_RERANKER_API_KEY=local-reranker        # 任意非空值（本服务不校验）
    STOCK_MEMORY_RERANKER_API_BASE=http://127.0.0.1:8866/v1
    STOCK_MEMORY_RERANKER_MODEL=BAAI/bge-reranker-v2-m3

运行：
    cd memory/reranker
    copy .env.example .env   （按需修改端口/设备）
    python server.py
    # 或： uvicorn server:app --host 0.0.0.0 --port 8866

接口：
    POST /v1/rerank  (OpenAI 兼容)
    POST /rerank      (别名)
    GET  /health      {status, model, device, loaded, load_error}
"""

import ctypes
import gc
import logging
import os
import subprocess
import sys
import threading
import time
from typing import List, Optional

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict
from dotenv import load_dotenv

load_dotenv()

_log = logging.getLogger("reranker_service")

# ── 配置（环境变量 / .env） ──
HOST = os.getenv("RERANKER_HOST", "0.0.0.0")
PORT = int(os.getenv("RERANKER_PORT", "8866"))
MODEL = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
# 本地模型目录候选（按顺序取第一个可用者）：
#   1) RERANKER_LOCAL_DIR
#   2) STOCK_MEMORY_RERANKER_LOCAL_DIR
#   3) ../../E:/Code/Lib/Model/bge-reranker-v2-m3（无法跨盘，仅同盘时命中）
#   4) ./model/bge-reranker-v2-m3（与 server.py 同目录）
def _resolve_local_dir() -> str:
    _here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.getenv("RERANKER_LOCAL_DIR", ""),
        os.getenv("STOCK_MEMORY_RERANKER_LOCAL_DIR", ""),
        os.path.join(_here, "model", "bge-reranker-v2-m3"),
    ]
    for c in candidates:
        if c and os.path.isdir(c) and (
            os.path.exists(os.path.join(c, "modules.json"))
            or os.path.exists(os.path.join(c, "config.json"))
        ):
            return c
    for c in candidates:
        if c:
            return c
    return ""

LOCAL_DIR = _resolve_local_dir()
DEVICE = os.getenv("RERANKER_DEVICE", "cpu")
MAX_DOC_LEN = int(os.getenv("RERANKER_MAX_DOC_LEN", "1024"))
IDLE_TTL = int(os.getenv("RERANKER_IDLE_TTL", "0"))

if not os.getenv("HF_ENDPOINT"):
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
    _log.info("未设置 HF_ENDPOINT，默认使用镜像: https://hf-mirror.com")

# ── 全局状态 ──
_model = None
_model_lock = threading.Lock()
_last_used = 0.0
_load_attempted = False
_load_error: Optional[str] = None


def _load_model() -> bool:
    global _model, _load_attempted, _load_error, _last_used
    with _model_lock:
        if _model is not None:
            return True
        if _load_attempted:
            return False
        _load_attempted = True
        try:
            from sentence_transformers import CrossEncoder
            if LOCAL_DIR and os.path.isdir(LOCAL_DIR) and (
                os.path.exists(os.path.join(LOCAL_DIR, "modules.json"))
                or os.path.exists(os.path.join(LOCAL_DIR, "config.json"))
            ):
                _log.info("从本地目录加载 reranker: %s", LOCAL_DIR)
                path = LOCAL_DIR
            else:
                if LOCAL_DIR:
                    _log.warning("本地目录不含模型文件，改从 HuggingFace 下载: %s", LOCAL_DIR)
                _log.info("加载 reranker 模型: %s (device=%s)", MODEL, DEVICE)
                path = MODEL
            _model = CrossEncoder(path, device=DEVICE)
            _last_used = time.time()
            _log.info("reranker 加载完成")
            return True
        except Exception as exc:
            _load_error = str(exc)
            _log.exception("reranker 加载失败")
            return False


def _ensure_model() -> bool:
    if _model is not None:
        return True
    return _load_model()


def _maybe_unload() -> None:
    global _model
    if IDLE_TTL <= 0 or _model is None:
        return
    if time.time() - _last_used > IDLE_TTL:
        with _model_lock:
            if _model is not None and time.time() - _last_used > IDLE_TTL:
                _log.info("空闲超过 %ds，卸载 reranker 权重", IDLE_TTL)
                _model = None
                gc.collect()
                if DEVICE.startswith("cuda"):
                    try:
                        import torch
                        torch.cuda.empty_cache()
                    except Exception:
                        pass


def _start_reaper() -> None:
    if IDLE_TTL <= 0:
        return
    def _loop() -> None:
        while True:
            time.sleep(15)
            try:
                _maybe_unload()
            except Exception:
                pass
    t = threading.Thread(target=_loop, name="reranker-reaper", daemon=True)
    t.start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _log.info("启动 reranker 服务: model=%s device=%s idle_ttl=%s port=%s", MODEL, DEVICE, IDLE_TTL, PORT)
    if IDLE_TTL <= 0:
        ok = _load_model()
        _log.info("预加载 %s", "成功" if ok else f"失败: {_load_error}")
    _start_reaper()
    yield
    _log.info("reranker 服务关闭")


app = FastAPI(title="Reranker Service", lifespan=lifespan)


class RerankRequest(BaseModel):
    model: str = ""
    query: str
    documents: List[str]
    top_n: Optional[int] = None
    return_documents: bool = False
    model_config = ConfigDict(extra="ignore")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": MODEL,
        "device": DEVICE,
        "loaded": _model is not None,
        "load_error": _load_error,
    }


@app.post("/v1/rerank")
@app.post("/rerank")
def rerank(req: RerankRequest):
    if not _ensure_model():
        raise HTTPException(status_code=503, detail=f"reranker 不可用: {_load_error}")

    docs = req.documents or []
    if not docs:
        return {"object": "list", "model": req.model or MODEL, "results": []}

    pairs = [[req.query, d[:MAX_DOC_LEN]] for d in docs]

    with _model_lock:
        m = _model
        if m is None:
            raise HTTPException(status_code=503, detail="reranker 正被卸载，请重试")
        scores = m.predict(pairs, show_progress_bar=False).tolist()

    _last_used = time.time()

    results = [{"index": i, "relevance_score": float(scores[i])} for i in range(len(scores))]
    if req.return_documents:
        for r, d in zip(results, docs):
            r["document"] = {"text": d}
    results.sort(key=lambda x: x["relevance_score"], reverse=True)
    if req.top_n:
        results = results[:req.top_n]

    return {
        "id": f"rerk_{int(time.time() * 1000)}",
        "object": "list",
        "created": int(time.time()),
        "model": req.model or MODEL,
        "results": results,
    }


# ── 端口占用自愈 ──
_PIDFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".reranker.pid")


def _pid_using_port(port: int) -> Optional[int]:
    try:
        res = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True, timeout=15,
        )
        out = res.stdout.decode("utf-8", errors="ignore")
    except Exception:
        return None
    for line in out.splitlines():
        cols = line.split()
        if len(cols) < 5:
            continue
        local, state, pid = cols[1], cols[3], cols[-1]
        if f":{port}" in local and "LISTEN" in state and pid.isdigit():
            return int(pid)
    return None


def _image_of(pid: int) -> str:
    try:
        res = subprocess.run(
            ["tasklist", "/fi", f"PID eq {pid}"],
            capture_output=True, timeout=15,
        )
        out = res.stdout.decode("utf-8", errors="ignore")
        for line in out.splitlines():
            if str(pid) in line:
                return line.split()[0].lower()
    except Exception:
        return ""
    return ""


def _read_pidfile() -> Optional[int]:
    try:
        with open(_PIDFILE, "r", encoding="utf-8") as f:
            txt = f.read().strip()
        return int(txt) if txt.isdigit() else None
    except Exception:
        return None


def _write_pidfile() -> None:
    try:
        with open(_PIDFILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def _force_kill(pid: int) -> bool:
    if os.name != "nt":
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        advapi32 = ctypes.windll.advapi32
        TOKEN_ADJUST_PRIVILEGES = 0x0020
        TOKEN_QUERY = 0x0008
        SE_PRIVILEGE_ENABLED = 0x00000002
        tok = ctypes.wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(),
            TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
            ctypes.byref(tok),
        ):
            return False
        luid = ctypes.wintypes.LUID()
        if not advapi32.LookupPrivilegeValueW(None, "SeDebugPrivilege", ctypes.byref(luid)):
            return False

        class _LuidAttr(ctypes.Structure):
            _fields_ = [("Luid", ctypes.wintypes.LUID), ("Attributes", ctypes.wintypes.DWORD)]
        class _TokenPriv(ctypes.Structure):
            _fields_ = [("PrivilegeCount", ctypes.wintypes.DWORD), ("Privileges", _LuidAttr)]

        tp = _TokenPriv()
        tp.PrivilegeCount = 1
        tp.Privileges.Luid = luid
        tp.Privileges.Attributes = SE_PRIVILEGE_ENABLED
        advapi32.AdjustTokenPrivileges(tok, False, ctypes.byref(tp), 0, None, None)
        kernel32.CloseHandle(tok)

        PROCESS_TERMINATE = 0x0001
        h = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not h:
            return False
        ok = kernel32.TerminateProcess(h, 1)
        kernel32.CloseHandle(h)
        return bool(ok)
    except Exception:
        return False


def _kill_and_wait(pid: int, port: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True, timeout=15,
        )
    except Exception as exc:
        _log.warning("调用 taskkill 终止 PID=%d 时出现异常: %s（将以端口是否释放为准）", pid, exc)

    for _ in range(10):
        if _pid_using_port(port) is None:
            _log.info("旧实例(PID=%d)已退出，端口 %d 已释放。", pid, port)
            return
        time.sleep(0.5)

    if _pid_using_port(port) == pid:
        _log.warning("taskkill 未能结束 PID=%d，尝试以 Debug 权限强杀...", pid)
        if _force_kill(pid):
            for _ in range(10):
                if _pid_using_port(port) is None:
                    _log.info("旧实例(PID=%d)已退出，端口 %d 已释放。", pid, port)
                    return
                time.sleep(0.5)

    if _pid_using_port(port) == pid:
        img = _image_of(pid)
        _log.error("已向旧实例(PID=%d, 映像=%s)发送终止信号，但端口 %d 仍被其占用。", pid, img or "?", port)
        _log.error("请手动结束它后再启动：")
        _log.error("  taskkill /PID %d /F", pid)
        sys.exit(1)
    _log.warning("端口 %d 仍被其它进程占用，但目标 PID=%d 已退出，将继续尝试绑定。", port, pid)


def _claim_port(port: int) -> None:
    pid = _pid_using_port(port)
    if pid is None:
        return
    if _read_pidfile() == pid:
        _log.warning("端口 %d 被本服务旧实例(PID=%d)占用，先终止再启动。", port, pid)
        _kill_and_wait(pid, port)
        return
    img = _image_of(pid)
    if "python" in img:
        _log.warning("端口 %d 被 python 进程(PID=%d)占用，判定为本服务旧实例，先终止再启动。", port, pid, img)
        _kill_and_wait(pid, port)
        return
    _log.error("端口 %d 已被其它进程(PID=%d, 映像=%s)占用，且无法确认它是本服务。", port, pid, img or "(未知)")
    _log.error("为避免误杀无关进程，已中止启动。请先手动释放该端口后再试。")
    sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _claim_port(PORT)
    _write_pidfile()
    uvicorn.run(app, host=HOST, port=PORT)
