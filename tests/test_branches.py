"""真实 HTTP 接口测试：uvicorn 子进程 + httpx 请求。"""
import subprocess
import sys
import threading

import httpx
import pytest

from server_util import ROOT, spawn, stop

C_GROUPS = {
    "groups": [
        {
            "group_id": "g1",
            "candidates": [
                {"candidate_id": "c1", "text": "山川",
                 "evidence": {"witness": "景印文渊阁本", "folio": 18}},
                {"candidate_id": "c2", "text": "山川",
                 "evidence": {"witness": "文津阁钞本", "folio": 18}},
                {"candidate_id": "c3", "text": "山州",
                 "evidence": {"witness": "足抄本", "folio": 18}},
            ],
        },
        {"group_id": "g2", "candidates": [
            {"candidate_id": "d1", "text": "河流",
             "evidence": {"witness": "景印文渊阁本", "folio": 19}},
        ]},
        {"group_id": "g3", "candidates": [
            {"candidate_id": "e1", "text": "日月",
             "evidence": {"witness": "底本", "folio": 20}},
        ]},
    ]
}


@pytest.fixture
def world(server):
    """构造基础世界：revision 0 + 四位馆员。"""
    http = server.client()
    http.post("/actors", json={"actor_id": "alice", "permissions": ["merge"]})
    http.post("/actors", json={"actor_id": "bob", "permissions": []})
    http.post("/actors", json={"actor_id": "carol", "permissions": []})
    http.post("/actors", json={"actor_id": "dana", "permissions": ["merge"]})
    r = http.post("/volumes/v1/groups", json=C_GROUPS, headers={"X-Actor-Id": "alice"})
    assert r.status_code == 201
    return server


def actor(server, actor_id):
    return {"X-Actor-Id": actor_id}


def decide(http, branch, group, decision, actor_id, candidate_id=None, key=None):
    headers = {"X-Actor-Id": actor_id}
    if key:
        headers["Idempotency-Key"] = key
    return http.post(
        f"/branches/{branch}/decisions",
        json={"group_id": group, "decision": decision, "candidate_id": candidate_id},
        headers=headers,
    )


def make_branch(http, branch_id, revision, actor_id):
    r = http.post("/branches", json={"volume_id": "v1", "base_revision": revision,
                                     "branch_id": branch_id},
                  headers={"X-Actor-Id": actor_id})
    assert r.status_code == 201, r.text
    return r.json()


def propose(http, branch, actor_id):
    r = http.post(f"/branches/{branch}/merge-proposals",
                  headers={"X-Actor-Id": actor_id})
    assert r.status_code == 201, r.text
    return r.json()


def seed_trunk_decision(world):
    """让主干 rev1 = alice 在 g1 上 accept c1、g3 上 defer。"""
    http = world.client()
    make_branch(http, "trunk-seed", 0, "alice")
    decide(http, "trunk-seed", "g1", "accept", "alice", "c1")
    decide(http, "trunk-seed", "g3", "defer", "alice")
    p = propose(http, "trunk-seed", "alice")
    r = http.post(f"/merge-proposals/{p['proposal_id']}/commit",
                  headers={"X-Actor-Id": "alice"})
    assert r.status_code == 200, r.text
    assert r.json()["new_revision"] == 1


def categories(proposal):
    return {i["group_id"]: i["category"] for i in proposal["items"]}


# ---------------------------------------------------------------------------


def test_branch_isolation_and_preserved_evidence(world):
    """分支隔离：accept/reject/defer 只影响本分支，原始异文依据随决定保留。"""
    seed_trunk_decision(world)
    http = world.client()
    make_branch(http, "b-bob", 1, "bob")
    make_branch(http, "b-carol", 1, "carol")

    r = decide(http, "b-bob", "g1", "accept", "bob", "c3")
    assert r.status_code == 201
    assert r.json()["evidence"] == {"witness": "足抄本", "folio": 18}
    assert decide(http, "b-bob", "g2", "reject", "bob").status_code == 201

    # carol 的分支看不到 bob 的任何决定，只看到主干基线。
    carol_view = http.get("/branches/b-carol").json()
    by_id = {g["group_id"]: g for g in carol_view["groups"]}
    assert by_id["g1"]["decision"] == {
        "decision": "accept", "candidate_id": "c1",
        "evidence": {"witness": "景印文渊阁本", "folio": 18},
        "decided_by": "alice",
        "decided_at": by_id["g1"]["decision"]["decided_at"],
        "origin": "base",
    }
    assert by_id["g2"]["decision"] is None
    assert by_id["g3"]["decision"]["decision"] == "defer"

    # bob 分支上 g1 是本分支决定；g3 未决则继续呈现基线 defer。
    bob_view = http.get("/branches/b-bob").json()
    by_id_b = {g["group_id"]: g for g in bob_view["groups"]}
    assert by_id_b["g1"]["decision"]["decision"] == "accept"
    assert by_id_b["g1"]["decision"]["candidate_id"] == "c3"
    assert by_id_b["g1"]["decision"]["origin"] == "branch"
    assert by_id_b["g1"]["decision"]["decided_by"] == "bob"
    assert by_id_b["g2"]["decision"]["decision"] == "reject"
    assert by_id_b["g3"]["decision"]["origin"] == "base"

    # 主干 revision 1 未受分支决定影响。
    rev1 = http.get("/volumes/v1/revisions/1").json()
    g1 = next(g for g in rev1["groups"] if g["group_id"] == "g1")
    assert g1["decision"]["candidate_id"] == "c1"

    # 分支必须从明确存在的 revision 建立。
    r = http.post("/branches", json={"volume_id": "v1", "base_revision": 99,
                                     "branch_id": "b-x"},
                  headers={"X-Actor-Id": "bob"})
    assert r.status_code == 404


def test_preview_three_categories(world):
    """合并预览逐项给出 自动合入 / 同内容异依据 / 决定冲突。"""
    seed_trunk_decision(world)
    http = world.client()
    make_branch(http, "b-exp", 1, "bob")
    # g1: 接受 c2——与主干 c1 文本相同（山川）但依据不同（文津阁钞本）
    decide(http, "b-exp", "g1", "accept", "bob", "c2")
    # g2: 主干未决 -> 自动合入
    decide(http, "b-exp", "g2", "accept", "bob", "d1")
    p = propose(http, "b-exp", "bob")
    cats = categories(p)
    assert cats == {"g1": "same_content_different_evidence", "g2": "auto_merge"}
    g1 = next(i for i in p["items"] if i["group_id"] == "g1")
    assert g1["source"]["evidence"]["witness"] == "文津阁钞本"
    assert g1["target"]["evidence"]["witness"] == "景印文渊阁本"

    # 改判 g1 为接受不同文本 c3 -> 决定冲突
    decide(http, "b-exp", "g1", "accept", "bob", "c3")
    p2 = propose(http, "b-exp", "bob")
    assert categories(p2)["g1"] == "conflict"

    # defer/accept 之间也算冲突
    decide(http, "b-exp", "g1", "defer", "bob")
    p3 = propose(http, "b-exp", "bob")
    assert categories(p3)["g1"] == "conflict"


def test_stale_proposal_rejected(world):
    """固定水位的提案在目标 revision 前进后过期，不得生效。"""
    seed_trunk_decision(world)
    http = world.client()
    # bob 的提案固定在 target=1。
    make_branch(http, "b-bob", 1, "bob")
    decide(http, "b-bob", "g2", "accept", "bob", "d1")
    stale = propose(http, "b-bob", "bob")
    assert stale["target_watermark"] == 1

    # bob 的冲突提案同样固定在 target=1（在主干前进之前生成）。
    make_branch(http, "b-conflict", 1, "bob")
    decide(http, "b-conflict", "g1", "accept", "bob", "c3")
    stale2 = propose(http, "b-conflict", "bob")

    # carol 的分支先合入，把主干推进到 revision 2（无冲突项）。
    make_branch(http, "b-carol", 1, "carol")
    decide(http, "b-carol", "g2", "accept", "carol", "d1")
    p_carol = propose(http, "b-carol", "carol")
    r = http.post(f"/merge-proposals/{p_carol['proposal_id']}/commit",
                  headers={"X-Actor-Id": "dana"})
    assert r.status_code == 200
    assert r.json()["new_revision"] == 2

    # bob 的过期提案提交必须被拒绝，revision 不再前进。
    r = http.post(f"/merge-proposals/{stale['proposal_id']}/commit",
                  headers={"X-Actor-Id": "dana"})
    assert r.status_code == 409
    assert "过期" in r.json()["detail"]
    assert http.get("/volumes/v1").json()["head_revision"] == 2

    # 过期提案上连冲突裁决也不允许。
    r = http.post(f"/merge-proposals/{stale2['proposal_id']}/conflicts/g1",
                  json={"decision": "accept", "candidate_id": "c3"},
                  headers={"X-Actor-Id": "dana"})
    assert r.status_code == 409

    # 源分支在提案后又追加决定 -> 源水位过期。
    make_branch(http, "b-move", 2, "bob")
    decide(http, "b-move", "g2", "reject", "bob")
    p_move = propose(http, "b-move", "bob")
    decide(http, "b-move", "g3", "accept", "bob", "e1")
    r = http.post(f"/merge-proposals/{p_move['proposal_id']}/commit",
                  headers={"X-Actor-Id": "dana"})
    assert r.status_code == 409


def test_conflict_resolution_permissions(world):
    """冲突逐项裁决：需要 merge 权限，且裁决人未参与任何一方原决定。"""
    seed_trunk_decision(world)
    http = world.client()
    make_branch(http, "b-bob", 1, "bob")
    decide(http, "b-bob", "g1", "accept", "bob", "c3")
    p = propose(http, "b-bob", "bob")
    assert categories(p)["g1"] == "conflict"
    url = f"/merge-proposals/{p['proposal_id']}/conflicts/g1"
    body = {"decision": "accept", "candidate_id": "c3"}

    # 无 merge 权限：bob（源决定人）、carol 均被拒。
    assert http.post(url, json=body, headers=actor(world, "bob")).status_code == 403
    assert http.post(url, json=body, headers=actor(world, "carol")).status_code == 403
    # 有 merge 权限但是目标方原决定人：alice 被拒。
    assert http.post(url, json=body, headers=actor(world, "alice")).status_code == 403
    # dana 有 merge 权限且未参与原决定 -> 允许。
    r = http.post(url, json=body, headers=actor(world, "dana"))
    assert r.status_code == 201

    # 未裁决完不得合并。
    make_branch(http, "b-two", 1, "bob")
    decide(http, "b-two", "g1", "accept", "bob", "c3")
    decide(http, "b-two", "g3", "accept", "bob", "e1")  # 与主干 defer 冲突
    p2 = propose(http, "b-two", "bob")
    assert sorted(categories(p2)) == ["g1", "g3"]
    http.post(f"/merge-proposals/{p2['proposal_id']}/conflicts/g1",
              json=body, headers=actor(world, "dana"))
    r = http.post(f"/merge-proposals/{p2['proposal_id']}/commit",
                  headers={"X-Actor-Id": "dana"})
    assert r.status_code == 409 and "未裁决" in r.json()["detail"]
    http.post(f"/merge-proposals/{p2['proposal_id']}/conflicts/g3",
              json={"decision": "accept", "candidate_id": "e1"},
              headers=actor(world, "dana"))
    r = http.post(f"/merge-proposals/{p2['proposal_id']}/commit",
                  headers={"X-Actor-Id": "dana"})
    assert r.status_code == 200
    applied = {a["group_id"]: a for a in r.json()["applied"]}
    assert applied["g1"]["decision"] == "accept"
    assert applied["g3"]["decision"] == "accept"


def test_concurrent_merges_only_one_advances(world):
    """两个合并操作并发争用同一目标卷册：恰好一个推进 revision。"""
    seed_trunk_decision(world)  # head = 1
    http = world.client()
    for name, who in [("b-x", "bob"), ("b-y", "carol")]:
        make_branch(http, name, 1, who)
        decide(http, name, "g2", "accept", who, "d1")
    px = propose(http, "b-x", "bob")
    py = propose(http, "b-y", "carol")
    assert px["target_watermark"] == py["target_watermark"] == 1

    outcomes: list[tuple[str, httpx.Response]] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def commit(pid):
        try:
            with world.client() as one:
                r = one.post(f"/merge-proposals/{pid}/commit",
                             headers={"X-Actor-Id": "dana"})
            with lock:
                outcomes.append((pid, r))
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    t1 = threading.Thread(target=commit, args=(px["proposal_id"],))
    t2 = threading.Thread(target=commit, args=(py["proposal_id"],))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errors, errors
    statuses = sorted(r.status_code for _, r in outcomes)
    assert statuses == [200, 409], statuses
    body = http.get("/volumes/v1").json()
    assert body["head_revision"] == 2

    # 失败方重新提案：目标水位对齐到 2，g2 结论与依据一致，可再次合入推进到 rev3。
    failed_pid = next(pid for pid, r in outcomes if r.status_code == 409)
    loser = "b-x" if failed_pid == px["proposal_id"] else "b-y"
    loser_actor = "bob" if loser == "b-x" else "carol"
    p_retry = propose(http, loser, loser_actor)
    assert p_retry["target_watermark"] == 2
    assert categories(p_retry)["g2"] == "auto_merge"
    r = http.post(f"/merge-proposals/{p_retry['proposal_id']}/commit",
                  headers={"X-Actor-Id": "dana"})
    assert r.status_code == 200, r.text
    assert r.json()["new_revision"] == 3


def test_idempotent_replays(world):
    """相同 idempotency_key 重试返回首次结果（决定与合并提交）。"""
    seed_trunk_decision(world)
    http = world.client()
    make_branch(http, "b-idem", 1, "bob")
    headers = {"X-Actor-Id": "bob", "Idempotency-Key": "dec-1"}
    payload = {"group_id": "g2", "decision": "accept", "candidate_id": "d1"}
    r1 = http.post("/branches/b-idem/decisions", json=payload, headers=headers)
    r2 = http.post("/branches/b-idem/decisions", json=payload, headers=headers)
    assert r1.status_code == r2.status_code == 201
    assert r1.json() == r2.json()
    # 只落了一条决定事件。
    decisions = [e for e in http.get("/volumes/v1/events").json()
                 if e["kind"] == "decision" and e["branch_id"] == "b-idem"]
    assert len(decisions) == 1

    # 合并提交重放：第二次直接返回首次成功，而不是 409 已终结。
    p = propose(http, "b-idem", "bob")
    c_headers = {"X-Actor-Id": "dana", "Idempotency-Key": "commit-1"}
    c1 = http.post(f"/merge-proposals/{p['proposal_id']}/commit", headers=c_headers)
    c2 = http.post(f"/merge-proposals/{p['proposal_id']}/commit", headers=c_headers)
    assert c1.status_code == c2.status_code == 200
    assert c1.json() == c2.json()
    assert http.get("/volumes/v1").json()["head_revision"] == 2


def test_lineage_events_audit_and_cursor(world):
    """决定、谱系、审计事件与事件游标一致推进。"""
    seed_trunk_decision(world)
    http = world.client()
    status = http.get("/volumes/v1").json()
    events = http.get("/volumes/v1/events").json()
    assert status["event_cursor"] == events[-1]["seq"]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))

    lineage = http.get("/volumes/v1/lineage").json()
    assert [(r["revision"], r["parent_revision"], r["origin"]) for r in lineage] == [
        (0, -1, "seed"), (1, 0, "merge"),
    ]

    audit = http.get("/volumes/v1/audit").json()
    actions = [a["action"] for a in audit]
    assert "commit_merge" in actions and "decide" in actions
    # 每条审计都记录了同事务内业务事件的序号。
    merge_audit = next(a for a in audit if a["action"] == "commit_merge")
    assert merge_audit["event_seq"] is not None
    assert merge_audit["detail"]["new_revision"] == 1

    # 游标分页读取。
    first = http.get("/volumes/v1/events", params={"after": 1}).json()
    assert all(e["seq"] > 1 for e in first)



def test_restart_then_historical_revision_view(tmp_path):
    """重启服务后按旧 revision 回看，仍能还原当时的候选与决定。"""
    db_path = tmp_path / "restart.sqlite3"

    proc, url = spawn(db_path)
    try:
        with httpx.Client(base_url=url, timeout=30) as http:
            http.post("/actors", json={"actor_id": "alice", "permissions": ["merge"]})
            http.post("/actors", json={"actor_id": "dana", "permissions": ["merge"]})
            http.post("/actors", json={"actor_id": "bob", "permissions": []})
            http.post("/volumes/v1/groups", json=C_GROUPS,
                      headers={"X-Actor-Id": "alice"})
            make_branch(http, "b0", 0, "alice")
            decide(http, "b0", "g1", "accept", "alice", "c1")
            p = propose(http, "b0", "alice")
            http.post(f"/merge-proposals/{p['proposal_id']}/commit",
                      headers={"X-Actor-Id": "alice"})
            make_branch(http, "b1", 1, "bob")
            decide(http, "b1", "g1", "accept", "bob", "c3")
            p1 = propose(http, "b1", "bob")
            http.post(f"/merge-proposals/{p1['proposal_id']}/conflicts/g1",
                      json={"decision": "accept", "candidate_id": "c3"},
                      headers={"X-Actor-Id": "dana"})
            http.post(f"/merge-proposals/{p1['proposal_id']}/commit",
                      headers={"X-Actor-Id": "dana"})
            assert http.get("/volumes/v1").json()["head_revision"] == 2
    finally:
        stop(proc)

    # 重启：同一份 DB 文件，全新进程与动态端口。
    proc2, url2 = spawn(db_path)
    try:
        with httpx.Client(base_url=url2, timeout=30) as http:
            rev0 = http.get("/volumes/v1/revisions/0").json()
            rev1 = http.get("/volumes/v1/revisions/1").json()
            rev2 = http.get("/volumes/v1/revisions/2").json()
            assert rev0["groups"][0]["decision"] is None
            g1_rev1 = next(g for g in rev1["groups"] if g["group_id"] == "g1")
            assert g1_rev1["decision"]["candidate_id"] == "c1"
            assert g1_rev1["decision"]["evidence"]["witness"] == "景印文渊阁本"
            g1_rev2 = next(g for g in rev2["groups"] if g["group_id"] == "g1")
            assert g1_rev2["decision"]["candidate_id"] == "c3"
            assert g1_rev2["decision"]["decided_by"] == "dana"
            # 候选与原始依据在每个历史 revision 上都完整可还原。
            texts = {c["candidate_id"]: c["text"] for c in g1_rev2["candidates"]}
            assert texts == {"c1": "山川", "c2": "山川", "c3": "山州"}
            lineage = http.get("/volumes/v1/lineage").json()
            assert [r["revision"] for r in lineage] == [0, 1, 2]
            assert lineage[2]["parent_revision"] == 1
            # 事件与游标也随持久化恢复。
            status = http.get("/volumes/v1").json()
            events = http.get("/volumes/v1/events").json()
            assert status["event_cursor"] == events[-1]["seq"]
    finally:
        stop(proc2)


def test_migrations_are_repeatable(tmp_path):
    """迁移命令可重复执行，且服务能直接在迁移后的库上启动。"""
    import os

    db_path = tmp_path / "mig.sqlite3"
    env = os.environ.copy()
    env["COLLATION_DB_PATH"] = str(db_path)
    for _ in range(3):
        r = subprocess.run([sys.executable, "-m", "collation.migrate"],
                           cwd=str(ROOT), env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    proc, url = spawn(db_path)
    try:
        with httpx.Client(base_url=url, timeout=30) as http:
            r = http.post("/actors", json={"actor_id": "a", "permissions": []})
            assert r.status_code == 201
    finally:
        stop(proc)
