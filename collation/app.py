import hashlib
import json
import uuid
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .db import append_audit, append_event, migrate, now, read_connection, write_connection

app = FastAPI(title="古籍异文汇校服务")


# --------------------------------------------------------------------------- 数据模型


class CandidateIn(BaseModel):
    candidate_id: str
    text: str
    evidence: dict | list | str | None = None  # 原始异文依据（出处、底本等）


class GroupIn(BaseModel):
    group_id: str
    candidates: list[CandidateIn]


class SeedBody(BaseModel):
    groups: list[GroupIn]


class SubmissionBody(BaseModel):
    submission_id: str
    volume_id: str
    page: int
    base_revision: int
    segments: list[dict]


class ActorBody(BaseModel):
    actor_id: str
    permissions: list[str] = Field(default_factory=list)


class BranchBody(BaseModel):
    volume_id: str
    base_revision: int
    branch_id: str | None = None


class DecisionBody(BaseModel):
    group_id: str
    decision: Literal["accept", "reject", "defer"]
    candidate_id: str | None = None
    rationale: str | None = None


class ConflictResolveBody(BaseModel):
    decision: Literal["accept", "reject", "defer"]
    candidate_id: str | None = None


# --------------------------------------------------------------------------- 工具函数


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value):
    return json.loads(value) if value is not None else None


def require_actor(db, actor_id: str | None, permission: str | None = None):
    if not actor_id:
        raise HTTPException(401, "缺少 X-Actor-Id")
    actor = db.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
    if not actor:
        raise HTTPException(401, "馆员未登记")
    if permission is not None and permission not in _loads(actor["permissions_json"]):
        raise HTTPException(403, f"缺少权限 {permission}")
    return actor


def get_volume(db, volume_id: str):
    volume = db.execute("SELECT * FROM volumes WHERE volume_id=?", (volume_id,)).fetchone()
    if not volume:
        raise HTTPException(404, "卷册不存在")
    return volume


def get_branch(db, branch_id: str):
    branch = db.execute("SELECT * FROM branches WHERE branch_id=?", (branch_id,)).fetchone()
    if not branch:
        raise HTTPException(404, "审校分支不存在")
    return branch


def groups_at(db, volume_id: str, revision: int) -> dict[str, dict]:
    rows = db.execute(
        "SELECT * FROM variant_groups WHERE volume_id=? AND revision=?",
        (volume_id, revision),
    ).fetchall()
    return {row["group_id"]: _loads(row["candidates_json"]) for row in rows}


def decisions_at(db, volume_id: str, revision: int) -> dict[str, dict]:
    rows = db.execute(
        "SELECT * FROM revision_decisions WHERE volume_id=? AND revision=?",
        (volume_id, revision),
    ).fetchall()
    return {
        row["group_id"]: {
            "decision": row["decision"],
            "candidate_id": row["candidate_id"],
            "evidence": _loads(row["evidence_json"]),
            "decided_by": row["decided_by"],
            "decided_at": row["decided_at"],
        }
        for row in rows
    }


def candidate_evidence(candidates: list[dict], candidate_id: str | None):
    if candidate_id is None:
        return None
    for candidate in candidates:
        if candidate["candidate_id"] == candidate_id:
            return candidate.get("evidence")
    return None


def idempotency_lookup(db, actor_id: str, key: str, request_hash: str):
    row = db.execute(
        "SELECT * FROM idempotency_keys WHERE actor_id=? AND idempotency_key=?",
        (actor_id, key),
    ).fetchone()
    if row is None:
        return None
    if row["request_hash"] != request_hash:
        raise HTTPException(422, "幂等键对应的请求体不一致")
    return row


def idempotency_store(db, actor_id: str, key: str, request_hash: str,
                      status_code: int, response: dict) -> None:
    db.execute(
        "INSERT OR REPLACE INTO idempotency_keys"
        "(actor_id,idempotency_key,request_hash,status_code,response_json,created_at)"
        " VALUES(?,?,?,?,?,?)",
        (actor_id, key, request_hash, status_code, _dumps(response), now()),
    )


def request_hash(*parts) -> str:
    return hashlib.sha256(_dumps(list(parts)).encode()).hexdigest()


# --------------------------------------------------------------------------- 生命周期


@app.on_event("startup")
def startup() -> None:
    migrate()


# --------------------------------------------------------------------------- 原有提交接口


@app.post("/submissions", status_code=201)
def submit(value: SubmissionBody):
    with write_connection() as db:
        old = db.execute(
            "SELECT * FROM submissions WHERE submission_id=?", (value.submission_id,)
        ).fetchone()
        body = _dumps(value.segments)
        if old:
            if old["segments_json"] != body:
                raise HTTPException(409, "submission_id 已被其他内容使用")
            return dict(old)
        db.execute(
            "INSERT INTO submissions VALUES(?,?,?,?,?,?)",
            (value.submission_id, value.volume_id, value.page,
             value.base_revision, body, now()),
        )
    return value.model_dump()


@app.get("/volumes/{volume_id}/submissions")
def list_submissions(volume_id: str):
    with read_connection() as db:
        return [
            dict(row)
            for row in db.execute(
                "SELECT * FROM submissions WHERE volume_id=? ORDER BY page,submission_id",
                (volume_id,),
            )
        ]


# --------------------------------------------------------------------------- 馆员登记


@app.post("/actors", status_code=201)
def register_actor(body: ActorBody, x_actor_id: str | None = Header(None)):
    with write_connection() as db:
        ts = now()
        db.execute(
            "INSERT INTO actors(actor_id,permissions_json,created_at) VALUES(?,?,?)"
            " ON CONFLICT(actor_id) DO UPDATE SET permissions_json=excluded.permissions_json",
            (body.actor_id, _dumps(body.permissions), ts),
        )
    return {"actor_id": body.actor_id, "permissions": body.permissions}


# --------------------------------------------------------------------------- 卷册与异文组


@app.post("/volumes/{volume_id}/groups", status_code=201)
def seed_groups(volume_id: str, body: SeedBody, x_actor_id: str | None = Header(None)):
    """在卷册 revision 0 落入首版异文组全量快照（候选 + 原始异文依据）。"""
    if not body.groups:
        raise HTTPException(422, "至少需要一个异文组")
    group_ids = [g.group_id for g in body.groups]
    if len(set(group_ids)) != len(group_ids):
        raise HTTPException(422, "异文组 id 重复")
    with write_connection() as db:
        if db.execute("SELECT 1 FROM volumes WHERE volume_id=?", (volume_id,)).fetchone():
            raise HTTPException(409, "卷册已初始化，revision 0 快照不可改写")
        ts = now()
        db.execute(
            "INSERT INTO volumes(volume_id,head_revision,event_cursor,created_at,updated_at)"
            " VALUES(?,0,0,?,?)",
            (volume_id, ts, ts),
        )
        db.execute(
            "INSERT INTO revisions(volume_id,revision,parent_revision,origin,created_by,"
            "detail_json,created_at) VALUES(?,0,-1,'seed',?,NULL,?)",
            (volume_id, x_actor_id, ts),
        )
        for group in body.groups:
            candidates = [c.model_dump(exclude_none=True) for c in group.candidates]
            ids = [c["candidate_id"] for c in candidates]
            if len(set(ids)) != len(ids):
                raise HTTPException(422, f"异文组 {group.group_id} 候选 id 重复")
            db.execute(
                "INSERT INTO variant_groups(volume_id,revision,group_id,candidates_json,created_at)"
                " VALUES(?,?,?,?,?)",
                (volume_id, 0, group.group_id, _dumps(candidates), ts),
            )
        seq = append_event(db, volume_id, "volume_seeded",
                           {"groups": group_ids})
        append_audit(db, volume_id, "seed_groups", x_actor_id,
                     {"groups": group_ids}, seq)
        db.execute("UPDATE volumes SET event_cursor=?,updated_at=? WHERE volume_id=?",
                   (seq, ts, volume_id))
    return {"volume_id": volume_id, "revision": 0, "groups": group_ids}


@app.get("/volumes/{volume_id}")
def volume_status(volume_id: str):
    with read_connection() as db:
        volume = get_volume(db, volume_id)
        return {
            "volume_id": volume_id,
            "head_revision": volume["head_revision"],
            "event_cursor": volume["event_cursor"],
        }


@app.get("/volumes/{volume_id}/revisions/{revision}")
def revision_view(volume_id: str, revision: int):
    """按旧 revision 回看：还原当时的全部候选（含依据）与决定。重启后同样有效。"""
    with read_connection() as db:
        rev = db.execute(
            "SELECT * FROM revisions WHERE volume_id=? AND revision=?",
            (volume_id, revision),
        ).fetchone()
        if not rev:
            raise HTTPException(404, "revision 不存在")
        groups = groups_at(db, volume_id, revision)
        decisions = decisions_at(db, volume_id, revision)
        return {
            "volume_id": volume_id,
            "revision": revision,
            "parent_revision": rev["parent_revision"],
            "origin": rev["origin"],
            "created_at": rev["created_at"],
            "groups": [
                {"group_id": gid, "candidates": candidates,
                 "decision": decisions.get(gid)}
                for gid, candidates in sorted(groups.items())
            ],
        }


@app.get("/volumes/{volume_id}/lineage")
def lineage(volume_id: str):
    with read_connection() as db:
        get_volume(db, volume_id)
        rows = db.execute(
            "SELECT * FROM revisions WHERE volume_id=? ORDER BY revision", (volume_id,)
        ).fetchall()
        return [
            {
                "revision": r["revision"],
                "parent_revision": r["parent_revision"],
                "origin": r["origin"],
                "created_by": r["created_by"],
                "detail": _loads(r["detail_json"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]


@app.get("/volumes/{volume_id}/events")
def list_events(volume_id: str, after: int = 0):
    """事件游标读取：返回 seq 大于 after 的事件。"""
    with read_connection() as db:
        get_volume(db, volume_id)
        rows = db.execute(
            "SELECT * FROM events WHERE volume_id=? AND seq>? ORDER BY seq",
            (volume_id, after),
        ).fetchall()
        return [
            {"seq": r["seq"], "kind": r["kind"], "branch_id": r["branch_id"],
             "payload": _loads(r["payload_json"]), "created_at": r["created_at"]}
            for r in rows
        ]


@app.get("/volumes/{volume_id}/audit")
def list_audit(volume_id: str):
    with read_connection() as db:
        get_volume(db, volume_id)
        rows = db.execute(
            "SELECT * FROM audit_log WHERE volume_id=? ORDER BY audit_id", (volume_id,)
        ).fetchall()
        return [
            {"audit_id": r["audit_id"], "action": r["action"], "actor": r["actor"],
             "branch_id": r["branch_id"], "detail": _loads(r["detail_json"]),
             "event_seq": r["event_seq"], "created_at": r["created_at"]}
            for r in rows
        ]


# --------------------------------------------------------------------------- 审校分支


@app.post("/branches", status_code=201)
def create_branch(body: BranchBody, x_actor_id: str = Header(...)):
    with write_connection() as db:
        require_actor(db, x_actor_id)
        get_volume(db, body.volume_id)
        rev = db.execute(
            "SELECT 1 FROM revisions WHERE volume_id=? AND revision=?",
            (body.volume_id, body.base_revision),
        ).fetchone()
        if not rev:
            raise HTTPException(404, "基线 revision 不存在，分支必须从明确的卷册 revision 建立")
        branch_id = body.branch_id or f"br-{uuid.uuid4().hex[:12]}"
        ts = now()
        try:
            db.execute(
                "INSERT INTO branches(branch_id,volume_id,base_revision,created_by,created_at,"
                "status,decision_cursor) VALUES(?,?,?,?,?,'open',0)",
                (branch_id, body.volume_id, body.base_revision, x_actor_id, ts),
            )
        except Exception:
            raise HTTPException(409, "分支 id 已存在")
        seq = append_event(db, body.volume_id, "branch_created",
                           {"branch_id": branch_id, "base_revision": body.base_revision},
                           branch_id)
        append_audit(db, body.volume_id, "create_branch", x_actor_id,
                     {"branch_id": branch_id, "base_revision": body.base_revision},
                     seq, branch_id)
        db.execute("UPDATE volumes SET event_cursor=?,updated_at=? WHERE volume_id=?",
                   (seq, ts, body.volume_id))
    return {"branch_id": branch_id, "volume_id": body.volume_id,
            "base_revision": body.base_revision, "decision_cursor": 0, "status": "open"}


@app.post("/branches/{branch_id}/decisions", status_code=201)
def decide(branch_id: str, body: DecisionBody,
           x_actor_id: str = Header(...),
           idempotency_key: str | None = Header(None)):
    """accept/reject/defer 只影响本分支；决定时复制并保留原始异文依据。"""
    rh = request_hash("decision", branch_id, body.model_dump())
    with write_connection() as db:
        require_actor(db, x_actor_id)
        stored = None
        if idempotency_key:
            # 重放优先：即使分支随后已合并关闭，也返回首次结果而不是报错。
            stored = idempotency_lookup(db, x_actor_id, idempotency_key, rh)
        if stored is None:
            branch = get_branch(db, branch_id)
            if branch["status"] != "open":
                raise HTTPException(409, "分支已关闭，不能再作决定")
            volume_id = branch["volume_id"]
            groups = groups_at(db, volume_id, branch["base_revision"])
            if body.group_id not in groups:
                raise HTTPException(404, "该异文组不在分支基线 revision 中")
            candidates = groups[body.group_id]
            if body.decision == "accept":
                if not body.candidate_id:
                    raise HTTPException(422, "accept 必须指定 candidate_id")
                if body.candidate_id not in [c["candidate_id"] for c in candidates]:
                    raise HTTPException(422, "candidate_id 不属于该异文组")
            elif body.candidate_id is not None:
                raise HTTPException(422, f"{body.decision} 不应指定 candidate_id")

            evidence = candidate_evidence(candidates, body.candidate_id)
            ts = now()
            db.execute(
                "INSERT INTO branch_decisions(branch_id,group_id,decision,candidate_id,"
                "evidence_json,rationale,decided_by,decided_at,updated_at,event_seq)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(branch_id,group_id) DO UPDATE SET"
                " decision=excluded.decision,candidate_id=excluded.candidate_id,"
                " evidence_json=excluded.evidence_json,rationale=excluded.rationale,"
                " updated_at=excluded.updated_at,event_seq=excluded.event_seq",
                (branch_id, body.group_id, body.decision, body.candidate_id,
                 _dumps(evidence), body.rationale, x_actor_id, ts, ts, None),
            )
            seq = append_event(
                db, volume_id, "decision",
                {"branch_id": branch_id, "group_id": body.group_id,
                 "decision": body.decision, "candidate_id": body.candidate_id,
                 "evidence": evidence, "rationale": body.rationale,
                 "decided_by": x_actor_id},
                branch_id,
            )
            db.execute(
                "UPDATE branch_decisions SET event_seq=? WHERE branch_id=? AND group_id=?",
                (seq, branch_id, body.group_id),
            )
            db.execute("UPDATE branches SET decision_cursor=? WHERE branch_id=?",
                       (seq, branch_id))
            append_audit(db, volume_id, "decide", x_actor_id,
                         {"branch_id": branch_id, "group_id": body.group_id,
                          "decision": body.decision, "candidate_id": body.candidate_id},
                         seq, branch_id)
            db.execute("UPDATE volumes SET event_cursor=?,updated_at=? WHERE volume_id=?",
                       (seq, ts, volume_id))
            result = {
                "branch_id": branch_id, "group_id": body.group_id,
                "decision": body.decision, "candidate_id": body.candidate_id,
                "evidence": evidence, "decided_by": x_actor_id,
                "decision_cursor": seq,
            }
            if idempotency_key:
                idempotency_store(db, x_actor_id, idempotency_key, rh, 201, result)
    if stored is not None:
        return JSONResponse(status_code=stored["status_code"],
                            content=_loads(stored["response_json"]))
    return result


@app.get("/branches/{branch_id}")
def branch_state(branch_id: str):
    """分支视图：基线候选与依据 + 分支自己的决定（含覆盖基线的情况）。"""
    with read_connection() as db:
        branch = get_branch(db, branch_id)
        volume_id = branch["volume_id"]
        base_revision = branch["base_revision"]
        groups = groups_at(db, volume_id, base_revision)
        base_decisions = decisions_at(db, volume_id, base_revision)
        branch_rows = db.execute(
            "SELECT * FROM branch_decisions WHERE branch_id=?", (branch_id,)
        ).fetchall()
        branch_decisions = {r["group_id"]: r for r in branch_rows}
        payload_groups = []
        for gid, candidates in sorted(groups.items()):
            row = branch_decisions.get(gid)
            if row:
                effective = {
                    "decision": row["decision"], "candidate_id": row["candidate_id"],
                    "evidence": _loads(row["evidence_json"]),
                    "decided_by": row["decided_by"], "decided_at": row["decided_at"],
                    "origin": "branch",
                }
            else:
                base = base_decisions.get(gid)
                effective = dict(base, origin="base") if base else None
            payload_groups.append({"group_id": gid, "candidates": candidates,
                                   "decision": effective})
        return {
            "branch_id": branch_id, "volume_id": volume_id,
            "base_revision": base_revision, "status": branch["status"],
            "decision_cursor": branch["decision_cursor"],
            "merged_revision": branch["merged_revision"],
            "groups": payload_groups,
        }


# --------------------------------------------------------------------------- 合并预览与提案


def compute_preview(db, branch, target_revision: int) -> list[dict]:
    volume_id = branch["volume_id"]
    target_groups = groups_at(db, volume_id, target_revision)
    source_groups = groups_at(db, volume_id, branch["base_revision"])
    target_decisions = decisions_at(db, volume_id, target_revision)
    branch_rows = db.execute(
        "SELECT * FROM branch_decisions WHERE branch_id=?", (branch["branch_id"],)
    ).fetchall()

    def candidate_index(groups):
        return {cid: cand for candidates in groups.values()
                for cand in candidates for cid in [cand["candidate_id"]]}

    source_index = candidate_index(source_groups)
    target_index = candidate_index(target_groups)

    items = []
    for row in sorted(branch_rows, key=lambda r: r["group_id"]):
        gid = row["group_id"]
        source = {
            "decision": row["decision"], "candidate_id": row["candidate_id"],
            "evidence": _loads(row["evidence_json"]),
            "decided_by": row["decided_by"],
        }
        target = target_decisions.get(gid)
        if target is None:
            category = "auto_merge"
            reason = "目标分支尚未决定，可直接采用源分支决定"
        else:
            target = {
                "decision": target["decision"], "candidate_id": target["candidate_id"],
                "evidence": target["evidence"], "decided_by": target["decided_by"],
            }
            if row["decision"] != target["decision"]:
                category = "conflict"
                reason = "源分支与目标分支处置不同（accept/reject/defer），须具备 merge 权限且未参与原决定者逐项裁决"
            elif row["decision"] == "accept":
                src_text = source_index.get(row["candidate_id"], {}).get("text")
                tgt_text = target_index.get(target["candidate_id"], {}).get("text")
                if src_text != tgt_text:
                    category = "conflict"
                    reason = ("双方虽都接受，但选定文本内容不同，须具备 merge 权限且未参与原决定者"
                              "逐项裁决")
                elif _dumps(source["evidence"]) != _dumps(target["evidence"]):
                    category = "same_content_different_evidence"
                    reason = ("选定文本内容相同，但所依据的异文出处不同；两套候选依据在卷册快照中"
                              "一并保留，合入不覆盖目标结论")
                else:
                    category = "auto_merge"
                    reason = "决定、选定文本与依据完全一致"
            else:
                category = "auto_merge"
                reason = "处置结论一致（reject/defer）"
        union_candidates = {}
        for cand in (source_groups.get(gid, []) + target_groups.get(gid, [])):
            union_candidates.setdefault(cand["candidate_id"], cand)
        items.append({
            "group_id": gid, "category": category, "reason": reason,
            "source": source, "target": target,
            "candidates": [union_candidates[k] for k in sorted(union_candidates)],
        })
    return items


@app.post("/branches/{branch_id}/merge-proposals", status_code=201)
def create_proposal(branch_id: str,
                    x_actor_id: str = Header(...),
                    idempotency_key: str | None = Header(None)):
    """提交合并提案：固定源分支水位（decision_cursor）与目标水位（head_revision）。"""
    with write_connection() as db:
        require_actor(db, x_actor_id)
        branch = get_branch(db, branch_id)
        if branch["status"] != "open":
            raise HTTPException(409, "分支已关闭，不能再提交合并提案")
        volume = get_volume(db, branch["volume_id"])
        volume_id = branch["volume_id"]
        target_watermark = volume["head_revision"]
        source_watermark = branch["decision_cursor"]
        # 幂等指纹包含水位：状态未变的重试返回首个提案，水位变化后复用同键则拒绝。
        rh = request_hash("proposal", branch_id, target_watermark, source_watermark)
        stored = None
        if idempotency_key:
            stored = idempotency_lookup(db, x_actor_id, idempotency_key, rh)
        if stored is None:
            items = compute_preview(db, branch, target_watermark)
            if not items:
                raise HTTPException(422, "分支上尚无任何决定，无可合并内容")
            conflicts = [i["group_id"] for i in items if i["category"] == "conflict"]
            proposal_id = f"mp-{uuid.uuid4().hex[:12]}"
            ts = now()
            preview_payload = _dumps({
                "target_watermark": target_watermark,
                "source_watermark": source_watermark,
                "items": items,
            })
            db.execute(
                "INSERT INTO merge_proposals(proposal_id,volume_id,source_branch,"
                "target_watermark,source_watermark,preview_json,status,created_by,created_at)"
                " VALUES(?,?,?,?,?,?,'open',?,?)",
                (proposal_id, volume_id, branch_id, target_watermark,
                 source_watermark, preview_payload, x_actor_id, ts),
            )
            seq = append_event(
                db, volume_id, "merge_proposed",
                {"proposal_id": proposal_id, "source_branch": branch_id,
                 "target_watermark": target_watermark,
                 "source_watermark": source_watermark, "conflicts": conflicts},
                branch_id,
            )
            append_audit(db, volume_id, "create_proposal", x_actor_id,
                         {"proposal_id": proposal_id, "target_watermark": target_watermark,
                          "source_watermark": source_watermark, "conflicts": conflicts},
                         seq, branch_id)
            db.execute("UPDATE volumes SET event_cursor=?,updated_at=? WHERE volume_id=?",
                       (seq, ts, volume_id))
            result = {
                "proposal_id": proposal_id, "volume_id": volume_id,
                "source_branch": branch_id,
                "target_watermark": target_watermark,
                "source_watermark": source_watermark,
                "status": "open", "conflicts": conflicts, "items": items,
            }
            if idempotency_key:
                idempotency_store(db, x_actor_id, idempotency_key, rh, 201, result)
    if stored is not None:
        return JSONResponse(status_code=stored["status_code"],
                            content=_loads(stored["response_json"]))
    return result


@app.get("/merge-proposals/{proposal_id}")
def get_proposal(proposal_id: str):
    with read_connection() as db:
        row = db.execute(
            "SELECT * FROM merge_proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "合并提案不存在")
        preview = _loads(row["preview_json"])
        resolutions = db.execute(
            "SELECT * FROM proposal_conflicts WHERE proposal_id=?", (proposal_id,)
        ).fetchall()
        resolved = {
            r["group_id"]: {
                "decision": r["decision"], "candidate_id": r["candidate_id"],
                "resolved_by": r["resolved_by"], "resolved_at": r["resolved_at"],
            }
            for r in resolutions
        }
        return {
            "proposal_id": proposal_id, "volume_id": row["volume_id"],
            "source_branch": row["source_branch"],
            "target_watermark": row["target_watermark"],
            "source_watermark": row["source_watermark"],
            "status": row["status"], "created_by": row["created_by"],
            "merge_revision": row["merge_revision"],
            "items": preview["items"],
            "resolved_conflicts": resolved,
        }


@app.post("/merge-proposals/{proposal_id}/conflicts/{group_id}", status_code=201)
def resolve_conflict(proposal_id: str, group_id: str, body: ConflictResolveBody,
                     x_actor_id: str = Header(...),
                     idempotency_key: str | None = Header(None)):
    """逐项裁决冲突：裁决人必须有 merge 权限且未参与该组任何一方的原决定。"""
    rh = request_hash("conflict", proposal_id, group_id, body.model_dump())
    with write_connection() as db:
        require_actor(db, x_actor_id, "merge")
        stored = None
        if idempotency_key:
            # 重放优先：提案随后过期或终结时，重试仍返回首次裁决结果。
            stored = idempotency_lookup(db, x_actor_id, idempotency_key, rh)
        if stored is None:
            proposal = db.execute(
                "SELECT * FROM merge_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if not proposal:
                raise HTTPException(404, "合并提案不存在")
            if proposal["status"] != "open":
                raise HTTPException(409, "提案已终结")
            volume_id = proposal["volume_id"]
            volume = get_volume(db, volume_id)
            # 过期预览上的裁决同样无效。
            if volume["head_revision"] != proposal["target_watermark"]:
                raise HTTPException(409, "提案目标水位已过期，请重新生成预览")
            branch = get_branch(db, proposal["source_branch"])
            if branch["decision_cursor"] != proposal["source_watermark"]:
                raise HTTPException(409, "源分支在提案后又有新决定，请重新生成预览")

            preview = _loads(proposal["preview_json"])
            item = next((i for i in preview["items"] if i["group_id"] == group_id), None)
            if item is None:
                raise HTTPException(404, "该异文组不属于本提案")
            if item["category"] != "conflict":
                raise HTTPException(422, "该异文组不是决定冲突项，无需裁决")
            if body.decision == "accept":
                if not body.candidate_id:
                    raise HTTPException(422, "accept 必须指定 candidate_id")
                valid = {c["candidate_id"] for c in item["candidates"]}
                if body.candidate_id not in valid:
                    raise HTTPException(422, "candidate_id 不属于该异文组")
            elif body.candidate_id is not None:
                raise HTTPException(422, f"{body.decision} 不应指定 candidate_id")

            participants = {item["source"]["decided_by"]}
            if item["target"]:
                participants.add(item["target"]["decided_by"])
            participants.discard(None)
            if x_actor_id in participants:
                raise HTTPException(403, "冲突裁决人不得是原决定参与人")

            existing = db.execute(
                "SELECT 1 FROM proposal_conflicts WHERE proposal_id=? AND group_id=?",
                (proposal_id, group_id),
            ).fetchone()
            if existing:
                raise HTTPException(409, "该冲突项已有裁决")
            ts = now()
            seq = append_event(
                db, volume_id, "conflict_resolved",
                {"proposal_id": proposal_id, "group_id": group_id,
                 "decision": body.decision, "candidate_id": body.candidate_id,
                 "resolved_by": x_actor_id},
                proposal["source_branch"],
            )
            db.execute(
                "INSERT INTO proposal_conflicts(proposal_id,group_id,decision,"
                "candidate_id,resolved_by,resolved_at,event_seq)"
                " VALUES(?,?,?,?,?,?,?)",
                (proposal_id, group_id, body.decision, body.candidate_id,
                 x_actor_id, ts, seq),
            )
            append_audit(db, volume_id, "resolve_conflict", x_actor_id,
                         {"proposal_id": proposal_id, "group_id": group_id,
                          "decision": body.decision, "candidate_id": body.candidate_id},
                         seq, proposal["source_branch"])
            db.execute("UPDATE volumes SET event_cursor=?,updated_at=? WHERE volume_id=?",
                       (seq, ts, volume_id))
            result = {
                "proposal_id": proposal_id, "group_id": group_id,
                "decision": body.decision, "candidate_id": body.candidate_id,
                "resolved_by": x_actor_id,
            }
            if idempotency_key:
                idempotency_store(db, x_actor_id, idempotency_key, rh, 201, result)
    if stored is not None:
        return JSONResponse(status_code=stored["status_code"],
                            content=_loads(stored["response_json"]))
    return result


@app.post("/merge-proposals/{proposal_id}/commit", status_code=200)
def commit_merge(proposal_id: str,
                 x_actor_id: str = Header(...),
                 idempotency_key: str | None = Header(None)):
    """提交合并：水位一致且冲突全部裁决后，推进唯一一个新 revision。"""
    with write_connection() as db:
        require_actor(db, x_actor_id, "merge")
        proposal = db.execute(
            "SELECT * FROM merge_proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        if not proposal:
            raise HTTPException(404, "合并提案不存在")
        volume_id = proposal["volume_id"]
        branch_id = proposal["source_branch"]
        branch = get_branch(db, branch_id)

        rh = request_hash("commit", proposal_id)
        stored = None
        if idempotency_key:
            old = idempotency_lookup(db, x_actor_id, idempotency_key, rh)
            if old:
                stored = old
        if stored is not None:
            result_status = stored["status_code"]
            result_body = _loads(stored["response_json"])
        else:
            if proposal["status"] != "open":
                raise HTTPException(409, "提案已终结")
            volume = get_volume(db, volume_id)
            # 过期预览不得生效：并发争用下只有水位仍匹配的一个事务能推进 revision。
            if volume["head_revision"] != proposal["target_watermark"]:
                raise HTTPException(409, "目标卷册已被其他合并推进，提案过期")
            if branch["status"] != "open" or branch["decision_cursor"] != proposal["source_watermark"]:
                raise HTTPException(409, "源分支水位已变化，提案过期")

            preview = _loads(proposal["preview_json"])
            resolution_rows = db.execute(
                "SELECT * FROM proposal_conflicts WHERE proposal_id=?", (proposal_id,)
            ).fetchall()
            resolutions = {r["group_id"]: r for r in resolution_rows}
            conflicts = [i["group_id"] for i in preview["items"]
                         if i["category"] == "conflict"]
            unresolved = [g for g in conflicts if g not in resolutions]
            if unresolved:
                raise HTTPException(409, f"尚有冲突项未裁决: {sorted(unresolved)}")

            target_rev = proposal["target_watermark"]
            new_rev = target_rev + 1
            ts = now()

            # 新 revision 全量快照：候选与依据取目标水位与源分支基线两边的并集，
            # 保证源分支带来而目标尚无的异文组/候选在合并后仍可回看。
            target_groups = groups_at(db, volume_id, target_rev)
            source_groups = groups_at(db, volume_id, branch["base_revision"])
            merged_groups: dict[str, list[dict]] = {}
            for groups in (target_groups, source_groups):
                for gid, candidates in groups.items():
                    bucket = merged_groups.setdefault(gid, {})
                    for cand in candidates:
                        bucket.setdefault(cand["candidate_id"], cand)
            target_decisions = decisions_at(db, volume_id, target_rev)
            merged_decisions: dict[str, dict] = {gid: dict(d) for gid, d in target_decisions.items()}
            apply_log = []
            for item in preview["items"]:
                gid = item["group_id"]
                category = item["category"]
                if category == "conflict":
                    r = resolutions[gid]
                    evidence = candidate_evidence(item["candidates"], r["candidate_id"])
                    merged_decisions[gid] = {
                        "decision": r["decision"], "candidate_id": r["candidate_id"],
                        "evidence": evidence, "decided_by": r["resolved_by"],
                        "decided_at": r["resolved_at"],
                    }
                    applied = {"group_id": gid, "category": category,
                               "decision": r["decision"], "candidate_id": r["candidate_id"]}
                elif gid not in target_decisions:
                    source = item["source"]
                    merged_decisions[gid] = {
                        "decision": source["decision"],
                        "candidate_id": source["candidate_id"],
                        "evidence": source["evidence"],
                        "decided_by": source["decided_by"],
                        "decided_at": ts,
                    }
                    applied = {"group_id": gid, "category": category,
                               "decision": source["decision"],
                               "candidate_id": source["candidate_id"]}
                else:
                    # 内容相同（依据无论是否不同）：目标结论已成立，保留目标决定。
                    applied = {"group_id": gid, "category": category,
                               "decision": target_decisions[gid]["decision"],
                               "candidate_id": target_decisions[gid]["candidate_id"]}
                apply_log.append(applied)

            for gid in sorted(merged_groups):
                candidates = [merged_groups[gid][k] for k in sorted(merged_groups[gid])]
                db.execute(
                    "INSERT INTO variant_groups(volume_id,revision,group_id,candidates_json,created_at)"
                    " VALUES(?,?,?,?,?)",
                    (volume_id, new_rev, gid, _dumps(candidates), ts),
                )
            for gid, decision in sorted(merged_decisions.items()):
                db.execute(
                    "INSERT INTO revision_decisions(volume_id,revision,group_id,decision,"
                    "candidate_id,evidence_json,decided_by,decided_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (volume_id, new_rev, gid, decision["decision"],
                     decision["candidate_id"], _dumps(decision["evidence"]),
                     decision["decided_by"], decision["decided_at"]),
                )
            db.execute(
                "INSERT INTO revisions(volume_id,revision,parent_revision,origin,created_by,"
                "detail_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (volume_id, new_rev, target_rev, "merge", x_actor_id,
                 _dumps({"proposal_id": proposal_id, "source_branch": branch_id,
                         "applied": apply_log}), ts),
            )
            seq = append_event(
                db, volume_id, "merged",
                {"proposal_id": proposal_id, "source_branch": branch_id,
                 "parent_revision": target_rev, "new_revision": new_rev,
                 "applied": apply_log},
                branch_id,
            )
            append_audit(db, volume_id, "commit_merge", x_actor_id,
                         {"proposal_id": proposal_id, "new_revision": new_rev,
                          "applied": apply_log}, seq, branch_id)
            db.execute(
                "UPDATE volumes SET head_revision=?,event_cursor=?,updated_at=?"
                " WHERE volume_id=?",
                (new_rev, seq, ts, volume_id),
            )
            db.execute(
                "UPDATE branches SET status='merged',merged_revision=? WHERE branch_id=?",
                (new_rev, branch_id),
            )
            db.execute(
                "UPDATE merge_proposals SET status='merged',resolved_by=?,resolved_at=?,"
                "merge_revision=? WHERE proposal_id=?",
                (x_actor_id, ts, new_rev, proposal_id),
            )
            result_body = {
                "proposal_id": proposal_id, "volume_id": volume_id,
                "source_branch": branch_id, "new_revision": new_rev,
                "parent_revision": target_rev, "applied": apply_log,
            }
            if idempotency_key:
                idempotency_store(db, x_actor_id, idempotency_key, rh, 200, result_body)
            result_status = 200
    return JSONResponse(status_code=result_status, content=result_body)
