"""真实 HTTP 接口测试：uvicorn 绑定 127.0.0.1:0（动态端口），httpx 走 TCP 访问。

覆盖：分支隔离、陈旧提案拒绝、并发合并唯一生效、重启后按旧 revision 回看、
冲突逐项裁决权限、幂等重试、迁移命令可重复。
"""
import os
import sqlite3
import subprocess
import sys
import threading
import time

import httpx
import pytest
import uvicorn


class RunningServer:
    """在后台线程运行真实 uvicorn 服务，端口由内核分配。"""

    def __init__(self):
        config = uvicorn.Config("collation.app:app", host="127.0.0.1", port=0,
                                log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(500):
            if self.server.started:
                break
            time.sleep(0.02)
        else:
            raise RuntimeError("服务未能启动")
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        assert self.port > 0
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{self.port}",
                                   timeout=30.0)

    def stop(self):
        self.client.close()
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLATION_DB_PATH", str(tmp_path / "test.sqlite3"))
    srv = RunningServer()
    yield srv
    srv.stop()


# ---------------------------------------------------------------- 小助手

def make_group(client, vid, page, skey, texts, actor="馆员甲"):
    resp = client.post(f"/volumes/{vid}/groups", json={
        "page": page, "segment_key": skey,
        "candidates": [{"text": t} for t in texts], "actor": actor})
    assert resp.status_code == 201, resp.text
    return resp.json()["group_id"]


def decide(client, vid, branch, group_id, action, actor, candidate_id=None,
           rationale="", key=None):
    body = {"group_id": group_id, "action": action, "actor": actor,
            "rationale": rationale}
    if candidate_id:
        body["candidate_id"] = candidate_id
    if key:
        body["idempotency_key"] = key
    return client.post(f"/volumes/{vid}/branches/{branch}/decisions", json=body)


def state(client, vid, branch, revision=None):
    params = {} if revision is None else {"revision": revision}
    resp = client.get(f"/volumes/{vid}/branches/{branch}/state", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def group_view(st, group_id):
    return next(g for g in st["groups"] if g["group_id"] == group_id)


def candidate_id(st, group_id, text):
    group = group_view(st, group_id)
    return next(c["candidate_id"] for c in group["candidates"] if c["text"] == text)


def make_branch(client, vid, branch_id, base_revision, actor="馆员甲"):
    resp = client.post(f"/volumes/{vid}/branches", json={
        "branch_id": branch_id, "base_revision": base_revision, "actor": actor})
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_proposal(client, vid, source, actor="馆员甲", key=None, target="main"):
    body = {"source_branch": source, "target_branch": target, "actor": actor}
    if key:
        body["idempotency_key"] = key
    return client.post(f"/volumes/{vid}/proposals", json=body)


def grant_merge(client, vid, user):
    resp = client.post(f"/volumes/{vid}/permissions",
                       json={"user_id": user, "permission": "merge"})
    assert resp.status_code == 201, resp.text


def merge(client, vid, proposal_id, actor, key=None):
    body = {"actor": actor}
    if key:
        body["idempotency_key"] = key
    return client.post(f"/volumes/{vid}/proposals/{proposal_id}/merge", json=body)


# ---------------------------------------------------------------- 分支隔离

def test_branch_isolation(server):
    c = server.client
    g1 = make_group(c, "v1", 1, "seg-1", ["山川", "山河"])
    g2 = make_group(c, "v1", 2, "seg-2", ["天地"])
    st = state(c, "v1", "main")
    assert st["head_revision"] == 2
    cand_shan = candidate_id(st, g1, "山川")

    # 主线决定 g1，随后从该水位建立地域专家分支
    assert decide(c, "v1", "main", g1, "accept", "馆员甲",
                  candidate_id=cand_shan, rationale="甲本").status_code == 201
    make_branch(c, "v1", "expert-a", base_revision=3)
    make_branch(c, "v1", "expert-b", base_revision=3)

    # 两个分支各自推进决定，互不影响，也不影响主线
    cand_tian = candidate_id(state(c, "v1", "expert-a"), g2, "天地")
    assert decide(c, "v1", "expert-a", g1, "reject", "专家A",
                  rationale="乙本疑误").status_code == 201
    assert decide(c, "v1", "expert-a", g2, "accept", "专家A",
                  candidate_id=cand_tian).status_code == 201
    assert decide(c, "v1", "expert-b", g1, "defer", "专家B",
                  rationale="待考").status_code == 201

    main_st = state(c, "v1", "main")
    assert group_view(main_st, g1)["decision"]["action"] == "accept"
    assert group_view(main_st, g1)["decision"]["actor"] == "馆员甲"
    assert group_view(main_st, g2)["decision"] is None

    a_st = state(c, "v1", "expert-a")
    assert group_view(a_st, g1)["decision"]["action"] == "reject"
    assert group_view(a_st, g2)["decision"]["action"] == "accept"

    b_st = state(c, "v1", "expert-b")
    assert group_view(b_st, g1)["decision"]["action"] == "defer"
    assert group_view(b_st, g2)["decision"] is None  # 看不到 expert-a 的决定

    # 主线新增候选（另一组未完成的考证），已建分支保持冻结
    resp = c.post(f"/volumes/v1/groups/{g1}/candidates",
                  json={"candidates": [{"text": "山川异体"}], "actor": "馆员甲"})
    assert resp.status_code == 201 and resp.json()["changed"] is True
    assert len(group_view(state(c, "v1", "main"), g1)["candidates"]) == 3
    assert len(group_view(state(c, "v1", "expert-a"), g1)["candidates"]) == 2

    # 原始异文依据在分支决定后完整保留
    texts = {cd["text"] for cd in group_view(a_st, g1)["candidates"]}
    assert texts == {"山川", "山河"}


# ---------------------------------------------------------------- 陈旧提案

def test_stale_proposal_rejected(server):
    c = server.client
    g1 = make_group(c, "v1", 1, "seg-1", ["山川"])           # main rev 1
    make_branch(c, "v1", "exp", base_revision=1)
    cand = candidate_id(state(c, "v1", "exp"), g1, "山川")
    decide(c, "v1", "exp", g1, "accept", "专家A", candidate_id=cand)  # 分支 rev 1
    grant_merge(c, "v1", "馆员丙")

    # 目标水位被后续主线写推进 → 提案过期
    p1 = make_proposal(c, "v1", "exp", key="prop-1").json()
    assert (p1["source_watermark"], p1["target_watermark"]) == (1, 1)
    make_group(c, "v1", 2, "seg-2", ["天地"])                # main rev 2
    resp = merge(c, "v1", p1["proposal_id"], "馆员丙")
    assert resp.status_code == 409 and "过期" in resp.json()["detail"]

    # 水位未变时重新提案可以合入
    p2 = make_proposal(c, "v1", "exp", key="prop-2").json()
    assert p2["target_watermark"] == 2
    assert merge(c, "v1", p2["proposal_id"], "馆员丙").status_code == 200

    # 源水位被分支后续决定推进 → 提案同样过期
    g3 = make_group(c, "v1", 3, "seg-3", ["日月"])           # 合并后主线再推进
    head = state(c, "v1", "main")["head_revision"]
    make_branch(c, "v1", "exp2", base_revision=head)
    cand3 = candidate_id(state(c, "v1", "exp2"), g3, "日月")
    decide(c, "v1", "exp2", g3, "accept", "专家A", candidate_id=cand3)
    p3 = make_proposal(c, "v1", "exp2", key="prop-3").json()
    decide(c, "v1", "exp2", g3, "defer", "专家A", rationale="再想想")
    resp = merge(c, "v1", p3["proposal_id"], "馆员丙")
    assert resp.status_code == 409 and "过期" in resp.json()["detail"]


# ---------------------------------------------------------------- 并发合并

def test_concurrent_merge_single_winner(server):
    c = server.client
    g1 = make_group(c, "v1", 1, "seg-1", ["山川"])
    g2 = make_group(c, "v1", 2, "seg-2", ["天地"])           # main rev 2
    make_branch(c, "v1", "exp-a", base_revision=2)
    make_branch(c, "v1", "exp-b", base_revision=2)
    st = state(c, "v1", "exp-a")
    decide(c, "v1", "exp-a", g1, "accept", "专家A",
           candidate_id=candidate_id(st, g1, "山川"))
    decide(c, "v1", "exp-b", g2, "accept", "专家B",
           candidate_id=candidate_id(st, g2, "天地"))
    grant_merge(c, "v1", "馆员丙")
    p1 = make_proposal(c, "v1", "exp-a", key="pa").json()["proposal_id"]
    p2 = make_proposal(c, "v1", "exp-b", key="pb").json()["proposal_id"]

    barrier = threading.Barrier(2)
    results = {}

    def do_merge(name, pid):
        client = httpx.Client(base_url=f"http://127.0.0.1:{server.port}", timeout=30.0)
        barrier.wait()
        results[name] = client.post(f"/volumes/v1/proposals/{pid}/merge",
                                    json={"actor": "馆员丙"}).status_code
        client.close()

    threads = [threading.Thread(target=do_merge, args=("m1", p1)),
               threading.Thread(target=do_merge, args=("m2", p2))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 两个合并争用同一目标卷册，只有一个推进 revision
    assert sorted(results.values()) == [200, 409]
    assert state(c, "v1", "main")["head_revision"] == 3

    # 落败方重新固定水位后仍可合入，两份决定最终都在主线
    loser = p2 if results["m1"] == 200 else p1
    source = "exp-b" if results["m1"] == 200 else "exp-a"
    fresh = make_proposal(c, "v1", source, key="retry").json()
    assert merge(c, "v1", fresh["proposal_id"], "馆员丙").status_code == 200
    final = state(c, "v1", "main")
    assert final["head_revision"] == 4
    assert group_view(final, g1)["decision"]["action"] == "accept"
    assert group_view(final, g2)["decision"]["action"] == "accept"
    assert loser != fresh["proposal_id"]


# ---------------------------------------------------------------- 重启回看

def test_restart_revision_lookback(tmp_path, monkeypatch):
    db_path = str(tmp_path / "restart.sqlite3")
    monkeypatch.setenv("COLLATION_DB_PATH", db_path)

    srv = RunningServer()
    c = srv.client
    g1 = make_group(c, "v1", 1, "seg-1", ["山川", "山河"])   # rev 1
    g2 = make_group(c, "v1", 2, "seg-2", ["天地"])           # rev 2
    st = state(c, "v1", "main")
    decide(c, "v1", "main", g1, "accept", "馆员甲",
           candidate_id=candidate_id(st, g1, "山川"), rationale="甲本")  # rev 3
    make_branch(c, "v1", "exp", base_revision=2)             # 看不到 rev 3 的决定
    decide(c, "v1", "exp", g2, "accept", "专家A",
           candidate_id=candidate_id(state(c, "v1", "exp"), g2, "天地"))
    srv.stop()

    # 重启后按旧 revision 回看，当时的候选与决定都要还原
    srv2 = RunningServer()
    try:
        c2 = srv2.client
        rev1 = state(c2, "v1", "main", revision=1)
        assert [g["group_id"] for g in rev1["groups"]] == [g1]
        assert {cd["text"] for cd in rev1["groups"][0]["candidates"]} == {"山川", "山河"}
        assert rev1["groups"][0]["decision"] is None

        rev2 = state(c2, "v1", "main", revision=2)
        assert len(rev2["groups"]) == 2
        assert all(g["decision"] is None for g in rev2["groups"])

        rev3 = state(c2, "v1", "main", revision=3)
        d = group_view(rev3, g1)["decision"]
        assert d["action"] == "accept" and d["rationale"] == "甲本"

        branch_st = state(c2, "v1", "exp")
        assert group_view(branch_st, g1)["decision"] is None      # 分支冻结在建支水位
        assert group_view(branch_st, g2)["decision"]["actor"] == "专家A"
    finally:
        srv2.stop()


# ---------------------------------------------------------------- 预览分类与裁决

def test_merge_preview_and_adjudication(server):
    c = server.client
    g1 = make_group(c, "v1", 1, "seg-1", ["甲一", "甲二"])
    g2 = make_group(c, "v1", 2, "seg-2", ["乙一"])
    g3 = make_group(c, "v1", 3, "seg-3", ["丙一", "丙二"])   # main rev 3
    st = state(c, "v1", "main")
    decide(c, "v1", "main", g1, "accept", "馆员甲",
           candidate_id=candidate_id(st, g1, "甲一"), rationale="甲本")
    decide(c, "v1", "main", g3, "accept", "馆员甲",
           candidate_id=candidate_id(st, g3, "丙一"), rationale="甲本")  # rev 5
    make_branch(c, "v1", "exp", base_revision=5)
    st = state(c, "v1", "exp")
    decide(c, "v1", "exp", g1, "accept", "专家乙",
           candidate_id=candidate_id(st, g1, "甲二"), rationale="乙本")   # 冲突
    decide(c, "v1", "exp", g2, "accept", "专家乙",
           candidate_id=candidate_id(st, g2, "乙一"), rationale="乙本")   # 自动合入
    decide(c, "v1", "exp", g3, "accept", "专家乙",
           candidate_id=candidate_id(st, g3, "丙一"), rationale="乙本补证")  # 同文异据

    proposal = make_proposal(c, "v1", "exp", key="p1").json()
    kinds = {item["group_id"]: item["kind"] for item in proposal["preview"]}
    assert kinds == {g1: "conflict", g2: "auto_merge",
                     g3: "same_content_different_evidence"}

    pid = proposal["proposal_id"]
    grant_merge(c, "v1", "馆员丙")
    # 冲突未裁决不得合入
    assert merge(c, "v1", pid, "馆员丙").status_code == 409
    # 裁决人须具备 merge 权限且未参与原决定
    assert c.post(f"/volumes/v1/proposals/{pid}/resolutions",
                  json={"group_id": g1, "resolution": "source",
                        "actor": "路人丁"}).status_code == 403
    assert c.post(f"/volumes/v1/proposals/{pid}/resolutions",
                  json={"group_id": g1, "resolution": "source",
                        "actor": "专家乙"}).status_code == 403
    assert c.post(f"/volumes/v1/proposals/{pid}/resolutions",
                  json={"group_id": g1, "resolution": "source",
                        "actor": "馆员甲"}).status_code == 403
    # 非冲突项不能裁决
    assert c.post(f"/volumes/v1/proposals/{pid}/resolutions",
                  json={"group_id": g2, "resolution": "source",
                        "actor": "馆员丙"}).status_code == 400
    assert c.post(f"/volumes/v1/proposals/{pid}/resolutions",
                  json={"group_id": g1, "resolution": "source",
                        "actor": "馆员丙"}).status_code == 201
    # 无 merge 权限不能执行合并
    assert merge(c, "v1", pid, "路人丁").status_code == 403

    result = merge(c, "v1", pid, "馆员丙", key="merge-1")
    assert result.status_code == 200, result.text
    final = state(c, "v1", "main")
    d1 = group_view(final, g1)["decision"]
    assert d1["action"] == "accept" and d1["actor"] == "专家乙"
    assert d1["origin_decision_id"] and d1["proposal_id"] == pid   # 谱系可追
    assert group_view(final, g2)["decision"]["rationale"] == "乙本"
    d3 = group_view(final, g3)["decision"]
    assert d3["rationale"] == "甲本"                                # 内容不变
    assert d3["linked_evidence"][0]["rationale"] == "乙本补证"      # 依据并入

    # 谱系接口能看到两条分支上的决定链
    lineage = c.get(f"/volumes/v1/groups/{g1}/lineage").json()["decisions"]
    assert {d["branch_id"] for d in lineage} == {"main", "exp"}

    # 审计事件与游标同事务推进
    events = c.get("/volumes/v1/events", params={"branch_id": "main"}).json()
    assert any(e["type"] == "merge_applied" for e in events["events"])
    db = sqlite3.connect(os.environ["COLLATION_DB_PATH"])
    cursor_row = db.execute(
        "SELECT head_revision,last_event_id FROM cursors"
        " WHERE volume_id='v1' AND branch_id='main'").fetchone()
    max_event = db.execute(
        "SELECT COALESCE(MAX(event_id),0) FROM events"
        " WHERE volume_id='v1' AND branch_id='main'").fetchone()[0]
    head = db.execute(
        "SELECT head_revision FROM branches"
        " WHERE volume_id='v1' AND branch_id='main'").fetchone()[0]
    db.close()
    assert cursor_row == (head, max_event) == (final["head_revision"], max_event)


# ---------------------------------------------------------------- 幂等

def test_idempotency_and_atomicity(server):
    c = server.client
    g1 = make_group(c, "v1", 1, "seg-1", ["山川"])
    make_branch(c, "v1", "exp", base_revision=1)
    cand = candidate_id(state(c, "v1", "exp"), g1, "山川")

    # 决定幂等：同 key 重试返回首次结果，水位只推进一次
    r1 = decide(c, "v1", "exp", g1, "accept", "专家A",
                candidate_id=cand, key="dec-1")
    r2 = decide(c, "v1", "exp", g1, "accept", "专家A",
                candidate_id=cand, key="dec-1")
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["decision_id"] == r2.json()["decision_id"]
    assert state(c, "v1", "exp")["head_revision"] == 1
    # 同 key 不同报文 → 冲突
    assert decide(c, "v1", "exp", g1, "reject", "专家A",
                  key="dec-1").status_code == 409

    # 提案幂等
    p1 = make_proposal(c, "v1", "exp", key="pk").json()
    p2 = make_proposal(c, "v1", "exp", key="pk").json()
    assert p1["proposal_id"] == p2["proposal_id"]
    assert len(c.get("/volumes/v1/proposals").json()) == 1

    # 合并幂等：重试返回首次结果，revision 只推进一次
    grant_merge(c, "v1", "馆员丙")
    m1 = merge(c, "v1", p1["proposal_id"], "馆员丙", key="mk")
    m2 = merge(c, "v1", p1["proposal_id"], "馆员丙", key="mk")
    assert m1.status_code == m2.status_code == 200
    assert m1.json() == m2.json()
    assert state(c, "v1", "main")["head_revision"] == 2
    # 已合入的提案换 key 再合并 → 拒绝
    assert merge(c, "v1", p1["proposal_id"], "馆员丙", key="mk2").status_code == 409

    # 失败的决定不产生任何落盘（同事务回滚）
    before = c.get("/volumes/v1/events").json()["next_cursor"]
    assert decide(c, "v1", "exp", g1, "accept", "专家A",
                  candidate_id="不存在的候选").status_code == 400
    after = c.get("/volumes/v1/events").json()["next_cursor"]
    assert before == after
    assert state(c, "v1", "exp")["head_revision"] == 1


# ---------------------------------------------------------------- 迁移命令

def test_migrate_command_repeatable(tmp_path):
    env = dict(os.environ, COLLATION_DB_PATH=str(tmp_path / "m.sqlite3"))
    for _ in range(2):
        run = subprocess.run([sys.executable, "-m", "collation.migrate"],
                             env=env, capture_output=True, text=True, cwd="/workspace")
        assert run.returncode == 0, run.stderr
    db = sqlite3.connect(tmp_path / "m.sqlite3")
    tables = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    db.close()
    assert {"branches", "decisions", "proposals", "events",
            "cursors", "idempotency"} <= tables
