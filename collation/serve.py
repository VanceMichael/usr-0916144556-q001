"""动态端口服务入口：绑定 127.0.0.1:0，由内核分配空闲端口并打印。

服务只监听本机回环地址，不访问外部网络。
"""
import threading
import time

import uvicorn


def main() -> None:
    config = uvicorn.Config("collation.app:app", host="127.0.0.1", port=0,
                            log_level="warning")
    server = uvicorn.Server(config)

    def report_port() -> None:
        while not server.started:
            time.sleep(0.05)
        for srv in server.servers:
            for sock in srv.sockets:
                host, port = sock.getsockname()[:2]
                print(f"汇校服务已启动: http://{host}:{port}", flush=True)

    threading.Thread(target=report_port, daemon=True).start()
    server.run()


if __name__ == "__main__":
    main()
