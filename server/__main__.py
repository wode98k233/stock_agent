"""启动 Web 服务。"""


def main():
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
