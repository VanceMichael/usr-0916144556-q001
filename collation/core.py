"""汇校领域逻辑：分支、决定链、合并提案、事件回放。

存储约定：
- 异文组与候选（原始异文依据）只挂在卷册主线 main 上，决定永不修改候选；
- 每个分支（含 main）有独立 revision 计数，分支另存建立时的主线水位 base_revision；
- 每次状态变化在同一事务内写入：业务行 + 审计事件 + 事件游标 + 分支水位；
- 任意 (branch, revision) 的状态由事件流回放还原，重启后依旧可回看。
"""
import hashlib
import json
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from .db import MAIN_BRANCH

ACTIONS = ("accept", "reject", "defer")
PERMISSION_MERGE = "merge"

# 合并预览差异类别
KIND_AUTO = "auto_merge"                          # 可自动合入
KIND_SAME_TEXT = "same_content_different_evidence"  # 内容相同但依据不同
KIND_CONFLICT = "conflict"                        # 决定冲突


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def request_hash(payload: dict) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 基础行读写

def ensure_main(db, volume_id: str):
    row = db.execute(
        "SELECT * FROM branches WHERE volume_id=? AND branch_id=?",
        (volume_id, MAIN_BRANCH),
    ).fetchone()
    if row:
        return row
    now = utcnow()
    db.execute(
        "INSERT INTO branches VALUES(?,?,?,?,?,?,?,?)",
        (volume_id, MAIN_BRANCH, "主线", None, None, 0, "system", now),
    )
    db.execute(
        "INSERT INTO cursors VALUES(?,?,?,?)", (volume_id, MAIN_BRANCH, 0, 0)
    )
    return db.execute(
        "SELECT * FROM branches WHERE volume_id=? AND branch_id=?",
        (volume_id, MAIN_BRANCH),
    ).fetchone()


def get_branch(db, volume_id: str, branch_id: str):
    return db.execute(
        "SELECT * FROM branches WHERE volume_id=? AND branch_id=?",
        (volume_id, branch_id),
    ).fetchone()


def require_branch(db, volume_id: str, branch_id: str):
    row = get_branch(db, volume_id, branch_id)
    if not row:
        raise HTTPException(404, f"分支不存在: {branch_id}")
    return row


def emit(db, volume_id, branch_id, branch_revision, type_, payload, actor) -> int:
    cur = db.execute(
        "INSERT INTO events(volume_id,branch_id,branch_revision,type,payload_json,actor,created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (
            volume_id, branch_id, branch_revision, type_,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            actor, utcnow(),
        ),
    )
    return cur.lastrowid


def advance(db, volume_id, branch_id, new_head, last_event_id, expected_head):
    """推进分支水位与事件游标；带乐观锁，水位不符即失败。"""
    cur = db.execute(
        "UPDATE branches SET head_revision=? WHERE volume_id=? AND branch_id=? AND head_revision=?",
        (new_head, volume_id, branch_id, expected_head),
    )
    if cur.rowcount != 1:
        raise HTTPException(409, "分支水位已变化，操作未生效")
    db.execute(
        "UPDATE cursors SET head_revision=?, last_event_id=? WHERE volume_id=? AND branch_id=?",
        (new_head, last_event_id, volume_id, branch_id),
    )


# ---------------------------------------------------------------- 幂等

def idem_lookup(db, key, endpoint, payload: dict):
    """相同 key 重试返回首次结果；同 key 不同报文视为冲突。"""
    if not key:
        return None
    row = db.execute("SELECT * FROM idempotency WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    if row["endpoint"] != endpoint or row["request_hash"] != request_hash(payload):
        raise HTTPException(409, "idempotency_key 已被其他请求使用")
    return {"status_code": row["status_code"], "body": json.loads(row["response_json"])}


def idem_store(db, key, endpoint, payload: dict, status_code: int, response: dict):
    if not key:
        return
    db.execute(
        "INSERT INTO idempotency VALUES(?,?,?,?,?,?)",
        (key, endpoint, request_hash(payload), status_code,
         json.dumps(response, ensure_ascii=False, sort_keys=True), utcnow()),
    )


# ---------------------------------------------------------------- 权限

def grant_permission(db, volume_id, user_id, permission):
    db.execute(
        "INSERT OR IGNORE INTO permissions VALUES(?,?,?,?)",
        (volume_id, user_id, permission, utcnow()),
    )


def has_permission(db, volume_id, user_id, permission) -> bool:
    return db.execute(
        "SELECT 1 FROM permissions WHERE volume_id=? AND user_id=? AND permission=?",
        (volume_id, user_id, permission),
    ).fetchone() is not None


def require_merge_permission(db, volume_id, user_id):
    if not has_permission(db, volume_id, user_id, PERMISSION_MERGE):
        raise HTTPException(403, f"用户 {user_id} 不具备 merge 权限")


# ---------------------------------------------------------------- 状态回放

def _apply_event(groups: dict, type_: str, payload: dict):
    if type_ == "group_created":
        groups[payload["group_id"]] = {
            "group_id": payload["group_id"],
            "page": payload["page"],
            "segment_key": payload["segment_key"],
            "candidates": list(payload["candidates"]),
            "decision": None,
        }
    elif type_ == "candidates_added":
        group = groups.get(payload["group_id"])
        if group is not None:
            known = {c["candidate_id"] for c in group["candidates"]}
            for cand in payload["candidates"]:
                if cand["candidate_id"] not in known:
                    group["candidates"].append(cand)
    elif type_ == "decision_made":
        group = groups.get(payload["group_id"])
        if group is not None:
            group["decision"] = payload["decision"]
    elif type_ == "evidence_linked":
        group = groups.get(payload["group_id"])
        if group is not None and group["decision"] is not None:
            group["decision"].setdefault("linked_evidence", []).append(payload["evidence"])


def replay(db, volume_id: str, branch, revision: int) -> list:
    """还原指定分支在指定 revision 的候选与决定。"""
    if branch["base_branch"] is not None:
        rows = db.execute(
            "SELECT * FROM events WHERE volume_id=? AND ("
            "  (branch_id=? AND branch_revision<=?) OR"
            "  (branch_id=? AND branch_revision<=?)"
            ") ORDER BY event_id",
            (volume_id, MAIN_BRANCH, branch["base_revision"],
             branch["branch_id"], revision),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM events WHERE volume_id=? AND branch_id=? AND branch_revision<=? ORDER BY event_id",
            (volume_id, MAIN_BRANCH, revision),
        ).fetchall()
    groups: dict = {}
    for row in rows:
        _apply_event(groups, row["type"], json.loads(row["payload_json"]))
    return [groups[key] for key in sorted(groups, key=lambda k: (groups[k]["page"], k))]


def state_view(db, volume_id: str, branch, revision: int) -> dict:
    if revision < 0 or revision > branch["head_revision"]:
        raise HTTPException(400, f"revision {revision} 超出分支水位 [0,{branch['head_revision']}]")
    return {
        "volume_id": volume_id,
        "branch_id": branch["branch_id"],
        "revision": revision,
        "head_revision": branch["head_revision"],
        "base_branch": branch["base_branch"],
        "base_revision": branch["base_revision"],
        "groups": replay(db, volume_id, branch, revision),
    }


# ---------------------------------------------------------------- 异文组与候选

def _candidate_row_view(row):
    return {
        "candidate_id": row["candidate_id"],
        "text": row["text"],
        "evidence": json.loads(row["evidence_json"]),
    }


def add_candidates_on_main(db, volume_id, page, segment_key, candidates, actor):
    """在主线异文组上追加候选；返回 (group_id, 是否有变化)。同事务由调用方保证。"""
    main = ensure_main(db, volume_id)
    group = db.execute(
        "SELECT * FROM variant_groups WHERE volume_id=? AND page=? AND segment_key=?",
        (volume_id, page, segment_key),
    ).fetchone()
    created = False
    if not group:
        group_id = new_id("grp")
        db.execute(
            "INSERT INTO variant_groups VALUES(?,?,?,?,?)",
            (group_id, volume_id, page, segment_key, utcnow()),
        )
        created = True
    else:
        group_id = group["group_id"]
    added = []
    for cand in candidates:
        text = cand["text"]
        exists = db.execute(
            "SELECT 1 FROM candidates WHERE group_id=? AND text=?", (group_id, text)
        ).fetchone()
        if exists:
            continue
        candidate_id = cand.get("candidate_id") or new_id("cand")
        evidence = cand.get("evidence") or []
        if isinstance(evidence, dict):
            evidence = [evidence]
        db.execute(
            "INSERT INTO candidates VALUES(?,?,?,?,?)",
            (candidate_id, group_id, text,
             json.dumps(evidence, ensure_ascii=False, sort_keys=True), utcnow()),
        )
        added.append({"candidate_id": candidate_id, "text": text, "evidence": evidence})
    if not added:
        return group_id, False
    new_head = main["head_revision"] + 1
    if created:
        event_type = "group_created"
        payload = {"group_id": group_id, "page": page,
                   "segment_key": segment_key, "candidates": added}
    else:
        event_type = "candidates_added"
        payload = {"group_id": group_id, "candidates": added}
    last_event = emit(db, volume_id, MAIN_BRANCH, new_head, event_type, payload, actor)
    advance(db, volume_id, MAIN_BRANCH, new_head, last_event, main["head_revision"])
    return group_id, True


# ---------------------------------------------------------------- 决定

def decision_view(row) -> dict:
    return {
        "decision_id": row["decision_id"],
        "volume_id": row["volume_id"],
        "branch_id": row["branch_id"],
        "group_id": row["group_id"],
        "action": row["action"],
        "candidate_id": row["candidate_id"],
        "rationale": row["rationale"],
        "actor": row["actor"],
        "branch_revision": row["branch_revision"],
        "origin_decision_id": row["origin_decision_id"],
        "supersedes_decision_id": row["supersedes_decision_id"],
        "proposal_id": row["proposal_id"],
        "created_at": row["created_at"],
    }


def current_decision_row(db, volume_id, branch_id, group_id):
    return db.execute(
        "SELECT d.* FROM current_decisions c JOIN decisions d ON d.decision_id=c.decision_id"
        " WHERE c.volume_id=? AND c.branch_id=? AND c.group_id=?",
        (volume_id, branch_id, group_id),
    ).fetchone()


def _insert_decision(db, volume_id, branch_id, group_id, action, candidate_id,
                     rationale, actor, branch_revision, origin_decision_id,
                     supersedes_decision_id, proposal_id) -> dict:
    decision_id = new_id("dec")
    now = utcnow()
    db.execute(
        "INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (decision_id, volume_id, branch_id, group_id, action, candidate_id,
         rationale, actor, branch_revision, origin_decision_id,
         supersedes_decision_id, proposal_id, now),
    )
    db.execute(
        "INSERT INTO current_decisions VALUES(?,?,?,?)"
        " ON CONFLICT(volume_id,branch_id,group_id) DO UPDATE SET decision_id=excluded.decision_id",
        (volume_id, branch_id, group_id, decision_id),
    )
    row = db.execute("SELECT * FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
    return decision_view(row)


def decide(db, volume_id, branch, group_id, action, candidate_id, rationale, actor) -> dict:
    if action not in ACTIONS:
        raise HTTPException(400, f"不支持的决定动作: {action}")
    if action == "accept" and not candidate_id:
        raise HTTPException(400, "accept 必须指定 candidate_id")
    if action != "accept" and candidate_id:
        raise HTTPException(400, f"{action} 不接受 candidate_id")
    branch_id = branch["branch_id"]
    groups = {g["group_id"]: g for g in replay(db, volume_id, branch, branch["head_revision"])}
    group = groups.get(group_id)
    if not group:
        raise HTTPException(404, "该分支上看不到此异文组")
    if action == "accept":
        known = {c["candidate_id"] for c in group["candidates"]}
        if candidate_id not in known:
            raise HTTPException(400, "candidate_id 不属于该异文组")
    current = current_decision_row(db, volume_id, branch_id, group_id)
    new_head = branch["head_revision"] + 1
    decision = _insert_decision(
        db, volume_id, branch_id, group_id, action, candidate_id, rationale,
        actor, new_head, None,
        current["decision_id"] if current else None, None,
    )
    last_event = emit(db, volume_id, branch_id, new_head, "decision_made",
                      {"group_id": group_id, "decision": decision}, actor)
    advance(db, volume_id, branch_id, new_head, last_event, branch["head_revision"])
    return decision


# ---------------------------------------------------------------- 分支

def create_branch(db, volume_id, branch_id, name, base_revision, actor) -> dict:
    main = ensure_main(db, volume_id)
    if base_revision is None:
        raise HTTPException(400, "必须显式给出 base_revision")
    if base_revision < 0 or base_revision > main["head_revision"]:
        raise HTTPException(400, f"base_revision 超出主线水位 [0,{main['head_revision']}]")
    if get_branch(db, volume_id, branch_id):
        raise HTTPException(409, f"分支已存在: {branch_id}")
    db.execute(
        "INSERT INTO branches VALUES(?,?,?,?,?,?,?,?)",
        (volume_id, branch_id, name or branch_id, MAIN_BRANCH, base_revision, 0, actor, utcnow()),
    )
    last_event = emit(db, volume_id, branch_id, 0, "branch_created",
                      {"branch_id": branch_id, "base_branch": MAIN_BRANCH,
                       "base_revision": base_revision}, actor)
    db.execute(
        "INSERT INTO cursors VALUES(?,?,?,?)", (volume_id, branch_id, 0, last_event)
    )
    return branch_view(get_branch(db, volume_id, branch_id), db)


def branch_view(row, db=None) -> dict:
    view = {
        "volume_id": row["volume_id"],
        "branch_id": row["branch_id"],
        "name": row["name"],
        "base_branch": row["base_branch"],
        "base_revision": row["base_revision"],
        "head_revision": row["head_revision"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }
    if db is not None:
        cursor = db.execute(
            "SELECT * FROM cursors WHERE volume_id=? AND branch_id=?",
            (row["volume_id"], row["branch_id"]),
        ).fetchone()
        if cursor:
            view["cursor"] = {"head_revision": cursor["head_revision"],
                              "last_event_id": cursor["last_event_id"]}
    return view


# ---------------------------------------------------------------- 合并提案

def _decided(decision):
    return decision if (decision and decision["action"] != "defer") else None


def build_preview(db, volume_id, source, target) -> list:
    source_groups = replay(db, volume_id, source, source["head_revision"])
    target_groups = replay(db, volume_id, target, target["head_revision"])
    target_decisions = {g["group_id"]: _decided(g["decision"]) for g in target_groups}
    items = []
    for group in source_groups:
        sd = _decided(group["decision"])
        if not sd:
            continue
        td = target_decisions.get(group["group_id"])
        if td is None:
            kind = KIND_AUTO
        elif (td["action"] == sd["action"]
              and (sd["action"] != "accept" or td["candidate_id"] == sd["candidate_id"])):
            if td["rationale"] == sd["rationale"]:
                continue  # 内容与依据完全一致，不是差异
            kind = KIND_SAME_TEXT
        else:
            kind = KIND_CONFLICT
        items.append({
            "group_id": group["group_id"],
            "kind": kind,
            "source_decision": sd,
            "target_decision": td,
        })
    return items


def proposal_view(row, db=None) -> dict:
    view = {
        "proposal_id": row["proposal_id"],
        "volume_id": row["volume_id"],
        "source_branch": row["source_branch"],
        "target_branch": row["target_branch"],
        "source_watermark": row["source_watermark"],
        "target_watermark": row["target_watermark"],
        "status": row["status"],
        "preview": json.loads(row["preview_json"]),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "merged_at": row["merged_at"],
        "merge_revision": row["merge_revision"],
    }
    if db is not None:
        view["resolutions"] = [
            {"group_id": r["group_id"], "resolution": r["resolution"],
             "rationale": r["rationale"], "actor": r["actor"], "created_at": r["created_at"]}
            for r in db.execute(
                "SELECT * FROM resolutions WHERE proposal_id=? ORDER BY group_id",
                (row["proposal_id"],),
            )
        ]
    return view


def create_proposal(db, volume_id, source_branch, target_branch, actor) -> dict:
    source = require_branch(db, volume_id, source_branch)
    target = require_branch(db, volume_id, target_branch)
    if source_branch == target_branch:
        raise HTTPException(400, "源分支与目标分支不能相同")
    items = build_preview(db, volume_id, source, target)
    proposal_id = new_id("prop")
    db.execute(
        "INSERT INTO proposals(proposal_id,volume_id,source_branch,target_branch,"
        "source_watermark,target_watermark,preview_json,status,created_by,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (proposal_id, volume_id, source_branch, target_branch,
         source["head_revision"], target["head_revision"],
         json.dumps(items, ensure_ascii=False, sort_keys=True),
         "open", actor, utcnow()),
    )
    row = db.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
    return proposal_view(row, db)


def require_proposal(db, volume_id, proposal_id):
    row = db.execute(
        "SELECT * FROM proposals WHERE volume_id=? AND proposal_id=?",
        (volume_id, proposal_id),
    ).fetchone()
    if not row:
        raise HTTPException(404, f"提案不存在: {proposal_id}")
    return row


def resolve_conflict(db, volume_id, proposal_id, group_id, resolution, rationale, actor):
    proposal = require_proposal(db, volume_id, proposal_id)
    if proposal["status"] != "open":
        raise HTTPException(409, "提案已关闭，无法裁决")
    if resolution not in ("source", "target"):
        raise HTTPException(400, "resolution 只能是 source 或 target")
    items = {item["group_id"]: item for item in json.loads(proposal["preview_json"])}
    item = items.get(group_id)
    if not item or item["kind"] != KIND_CONFLICT:
        raise HTTPException(400, "该异文组不是本提案的冲突项")
    require_merge_permission(db, volume_id, actor)
    participants = {item["source_decision"]["actor"]}
    if item["target_decision"]:
        participants.add(item["target_decision"]["actor"])
    if actor in participants:
        raise HTTPException(403, "裁决人不得参与原决定")
    db.execute(
        "INSERT INTO resolutions VALUES(?,?,?,?,?,?)"
        " ON CONFLICT(proposal_id,group_id) DO UPDATE SET"
        " resolution=excluded.resolution, rationale=excluded.rationale,"
        " actor=excluded.actor, created_at=excluded.created_at",
        (proposal_id, group_id, resolution, rationale, actor, utcnow()),
    )
    return {"proposal_id": proposal_id, "group_id": group_id,
            "resolution": resolution, "actor": actor}


def merge_proposal(db, volume_id, proposal_id, actor) -> dict:
    proposal = require_proposal(db, volume_id, proposal_id)
    if proposal["status"] == "merged":
        raise HTTPException(409, "提案已合入")
    require_merge_permission(db, volume_id, actor)
    source = require_branch(db, volume_id, proposal["source_branch"])
    target = require_branch(db, volume_id, proposal["target_branch"])
    if (source["head_revision"] != proposal["source_watermark"]
            or target["head_revision"] != proposal["target_watermark"]):
        raise HTTPException(409, "提案水位已过期：源或目标分支已推进，预览不得生效")
    items = json.loads(proposal["preview_json"])
    resolutions = {
        r["group_id"]: r for r in db.execute(
            "SELECT * FROM resolutions WHERE proposal_id=?", (proposal_id,))
    }
    unresolved = [i["group_id"] for i in items
                  if i["kind"] == KIND_CONFLICT and i["group_id"] not in resolutions]
    if unresolved:
        raise HTTPException(409, f"冲突项尚未裁决: {sorted(unresolved)}")

    target_id = proposal["target_branch"]
    new_head = target["head_revision"] + 1
    applied = []
    last_event = None
    for item in items:
        gid = item["group_id"]
        sd = item["source_decision"]
        kind = item["kind"]
        if kind == KIND_AUTO or (kind == KIND_CONFLICT
                                 and resolutions[gid]["resolution"] == "source"):
            current = current_decision_row(db, volume_id, target_id, gid)
            decision = _insert_decision(
                db, volume_id, target_id, gid, sd["action"], sd["candidate_id"],
                sd["rationale"], sd["actor"], new_head,
                sd["decision_id"],
                current["decision_id"] if current else None, proposal_id,
            )
            last_event = emit(db, volume_id, target_id, new_head, "decision_made",
                              {"group_id": gid, "decision": decision}, actor)
            applied.append({"group_id": gid, "kind": kind,
                            "result": "decision_merged", "decision_id": decision["decision_id"]})
        elif kind == KIND_SAME_TEXT:
            evidence = {"rationale": sd["rationale"], "from_decision_id": sd["decision_id"],
                        "from_branch": proposal["source_branch"]}
            last_event = emit(db, volume_id, target_id, new_head, "evidence_linked",
                              {"group_id": gid, "evidence": evidence}, actor)
            applied.append({"group_id": gid, "kind": kind,
                            "result": "evidence_linked"})
        else:  # 冲突且裁决保留目标
            last_event = emit(db, volume_id, target_id, new_head, "merge_kept_target",
                              {"group_id": gid, "kept_decision_id": item["target_decision"]["decision_id"],
                               "rejected_decision_id": sd["decision_id"]}, actor)
            applied.append({"group_id": gid, "kind": kind, "result": "kept_target"})
    last_event = emit(db, volume_id, target_id, new_head, "merge_applied",
                      {"proposal_id": proposal_id,
                       "source_branch": proposal["source_branch"],
                       "source_watermark": proposal["source_watermark"],
                       "applied": applied}, actor)
    advance(db, volume_id, target_id, new_head, last_event, target["head_revision"])
    db.execute(
        "UPDATE proposals SET status='merged', merged_at=?, merge_revision=? WHERE proposal_id=?",
        (utcnow(), new_head, proposal_id),
    )
    return {"proposal_id": proposal_id, "status": "merged",
            "merge_revision": new_head, "applied": applied}
