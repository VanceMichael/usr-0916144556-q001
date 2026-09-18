"""pytest 夹具：通过子进程启动真实 uvicorn 服务。"""
import pytest

from server_util import spawn, stop


class Server:
    def __init__(self, base_url: str, proc):
        self.base_url = base_url
        self.proc = proc

    def client(self):
        import httpx

        return httpx.Client(base_url=self.base_url, timeout=30)


@pytest.fixture
def make_server(tmp_path):
    servers: list[Server] = []

    def _make(name: str = "collation") -> Server:
        proc, url = spawn(tmp_path / f"{name}.sqlite3")
        server = Server(url, proc)
        servers.append(server)
        return server

    yield _make

    for server in servers:
        stop(server.proc)


@pytest.fixture
def server(make_server):
    return make_server()
