from fastapi.testclient import TestClient
from collation.app import app


def test_submission_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLATION_DB_PATH", str(tmp_path / "test.sqlite3"))
    with TestClient(app) as client:
        payload = {"submission_id":"s1","volume_id":"v1","page":1,"base_revision":0,"segments":[{"text":"山川","box":[1,2,3,4]}]}
        assert client.post("/submissions", json=payload).status_code == 201
        assert len(client.get("/volumes/v1/submissions").json()) == 1
