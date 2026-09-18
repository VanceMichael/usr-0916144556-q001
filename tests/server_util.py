"""测试辅助：在动态端口上启动真实 uvicorn 子进程（仅回环、无外部网络）。"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def spawn(db_path: Path, log_path: Path | None = None) -> tuple[subprocess.Popen, str]:
    """返回 (子进程, base_url)。日志写入文件，避免管道阻塞。"""
    port = free_port()
    log_path = log_path or db_path.with_suffix(".log")
    env = os.environ.copy()
    env["COLLATION_DB_PATH"] = str(db_path)
    log_file = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "collation.app:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=log_file, stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"服务提前退出，日志见 {log_path}")
        try:
            httpx.get(base_url + "/volumes/__health__", timeout=5)
            return proc, base_url
        except httpx.TransportError:
            time.sleep(0.1)
    proc.terminate()
    raise RuntimeError(f"服务健康检查失败，日志见 {log_path}")


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
