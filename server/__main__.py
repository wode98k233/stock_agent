"""启动 Web 服务。"""
import faulthandler
import logging
import os
import sys


def _enable_faulthandler():
    """开启原生崩溃现场记录。

    背景：本进程出现过多次「启动后突然消失、日志全空白」的现象，事后确认是
    Windows 原生访问违例（0xC0000005，故障模块 python311.dll，栈上有 sqlite3）。
    native 崩溃不会走 Python 异常机制，logging / try-except 一律抓不到，
    因此必须靠 faulthandler 把「崩溃线程的 Python 调用栈」写出来。

    - 默认写到项目日志文件（可用 SR_FAULTHANDLER_PATH 覆盖）；
    - 同时保留 stderr 输出（控制台/重定向文件）；
    - 失败绝不影响启动。
    """
    try:
        faulthandler.enable()  # 保底：至少 stderr 有栈
    except Exception:
        return
    try:
        from utils.app_paths import get_logs_dir
        path = os.environ.get(
            "SR_FAULTHANDLER_PATH",
            os.path.join(get_logs_dir(), "native_crash.log"),
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 追加写，保留历史崩溃现场；崩溃日志本身很小
        fh = open(path, "a", buffering=1, encoding="utf-8")
        faulthandler.enable(file=fh, all_threads=True)
        _fh_keepalive.append(fh)  # 防止被 GC 关掉
        logging.getLogger(__name__).debug(
            "[bootstrap] faulthandler 已开启，原生崩溃栈将写入 %s", path
        )
    except Exception:
        pass


_fh_keepalive = []


def main():
    _enable_faulthandler()
    try:
        import uvicorn
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "缺少 uvicorn，请先安装 requirements.txt 中的 Web 依赖。"
        ) from exc

    from config import Config
    from server.app import app

    # 并发连接上限。首页一次性并发请求 20+ 静态 JS，原先的 12 会被 uvicorn 以 503 拒掉。
    # 本地单用户工具无需严格限流，设宽松值即可（保留上限作为护栏）。
    # 注意：app 构造期（import server.app）已不再拉入重依赖；路由注册推迟到 lifespan，
    # 因此「命令 → Started server process」窗口仅含 uvicorn + 轻量 app 构造（约 1s）。
    uvicorn.run(app, host=Config.WEB_HOST, port=int(Config.WEB_PORT), reload=False, limit_concurrency=128)


if __name__ == "__main__":
    main()
