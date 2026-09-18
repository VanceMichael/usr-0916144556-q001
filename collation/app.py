import json
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import core
from .db import MAIN_BRANCH, connect, migrate, write_txn

app = FastAPI(title="古籍异文汇校服务")


class Submission(BaseModel):
    submission_id: str
    volume_id: str
    page: int
    base_revision: int
    segments: list[dict]


class CandidateIn(BaseModel):
    candidate_id: str | None = None
    text: str
    evidence: list | dict | None = None


class GroupCreate(BaseModel):
    page: int
    segment_key: str = ""
    candidates: list[CandidateIn]
    actor: str = "system"


class CandidatesAdd(BaseModel):
    candidates: list[CandidateIn]
    actor: str = "system"


class BranchCreate(BaseModel):
    branch_id: str
    name: str | None = None
    base_revision: int
    actor: str = "system"


class DecisionIn(BaseModel):
    group_id: str
    action: str
    candidate_id: str | None = None
    rationale: str = ""
    actor: str
    idempotency_key: str | None = None


class ProposalCreate(BaseModel):
    source_branch: str
    target_branch: str = MAIN_BRANCH
    actor: str
    idempotency_key: str | None = None


class ResolutionIn(BaseModel):
    group_id: str
    resolution: str
    rationale: str | None = None
    actor: str


class MergeIn(BaseModel):
    actor: str
    idempotency_key: str | None = None


class PermissionIn(BaseModel):
    user_id: str
    permission: str = core.PERMISSION_MERGE


@app.on_event("startup")
def startup() -> None:
    migrate()


# ---------------------------------------------------------------- 转写提交

@app.post("/submissions", status_code=201)
def submit(value: Submission):
    with write_txn() as db:
        old = db.execute(
            "SELECT * FROM submissions WHERE submission_id=?", (value.submission_id,)
        ).fetchone()
        body = json.dumps(value.segments, ensure_ascii=False, sort_keys=True)
        if old:
            if old["segments_json"] != body:
                raise HTTPException(409, "submission_id 已被其他内容使用")
            return dict(old)
        db.execute(
            "INSERT INTO submissions VALUES(?,?,?,?,?,?)",
            (value.submission_id, value.volume_id, value.page, value.base_revision,
             body, datetime.now(timezone.utc).isoformat()),
        )
        # 转写段落沉淀为主线上的异文候选（原始依据），重复文本不重复入账。
        for segment in value.segments:
            text = segment.get("text")
            if text is None:
                continue
            box = segment.get("box")
            segment_key = (json.dumps(box, ensure_ascii=False, sort_keys=True)
                           if box is not None else "")
            core.add_candidates_on_main(
                db, value.volume_id, value.page, segment_key,
                [{"text": text,
                  "evidence": [{"submission_id": value.submission_id, "box": box}]}],
                actor=f"submission:{value.submission_id}",
            )
    return value.model_dump()


@app.get("/volumes/{volume_id}/submissions")
def list_submissions(volume_id: str):
    with connect() as db:
        return [dict(row) for row in db.execute(
            "SELECT * FROM submissions WHERE volume_id=? ORDER BY page,submission_id",
            (volume_id,))]


# ---------------------------------------------------------------- 异文组

@app.post("/volumes/{volume_id}/groups", status_code=201)
def create_group(volume_id: str, value: GroupCreate):
    if not value.candidates:
        raise HTTPException(400, "至少需要一个候选")
    with write_txn() as db:
        group_id, changed = core.add_candidates_on_main(
            db, volume_id, value.page, value.segment_key,
            [c.model_dump() for c in value.candidates], value.actor)
        if not changed:
            raise HTTPException(409, "相同 page/segment_key 的异文组已存在且候选无新增")
        return {"group_id": group_id, "volume_id": volume_id}


@app.post("/volumes/{volume_id}/groups/{group_id}/candidates", status_code=201)
def add_candidates(volume_id: str, group_id: str, value: CandidatesAdd):
    with write_txn() as db:
        group = db.execute(
            "SELECT * FROM variant_groups WHERE volume_id=? AND group_id=?",
            (volume_id, group_id)).fetchone()
        if not group:
            raise HTTPException(404, "异文组不存在")
        _, changed = core.add_candidates_on_main(
            db, volume_id, group["page"], group["segment_key"],
            [c.model_dump() for c in value.candidates], value.actor)
        return {"group_id": group_id, "changed": changed}


# ---------------------------------------------------------------- 分支

@app.post("/volumes/{volume_id}/branches", status_code=201)
def create_branch(volume_id: str, value: BranchCreate):
    if value.branch_id == MAIN_BRANCH:
        raise HTTPException(400, "main 为保留分支名")
    with write_txn() as db:
        return core.create_branch(db, volume_id, value.branch_id, value.name,
                                  value.base_revision, value.actor)


@app.get("/volumes/{volume_id}/branches")
def list_branches(volume_id: str):
    with connect() as db:
        core.ensure_main(db, volume_id)
        return [core.branch_view(row) for row in db.execute(
            "SELECT * FROM branches WHERE volume_id=? ORDER BY created_at,branch_id",
            (volume_id,))]


@app.get("/volumes/{volume_id}/branches/{branch_id}")
def get_branch(volume_id: str, branch_id: str):
    with connect() as db:
        return core.branch_view(core.require_branch(db, volume_id, branch_id), db)


@app.get("/volumes/{volume_id}/branches/{branch_id}/state")
def branch_state(volume_id: str, branch_id: str, revision: int | None = None):
    """按 revision 回看：还原当时的候选与决定。缺省为分支当前水位。"""
    with connect() as db:
        branch = core.require_branch(db, volume_id, branch_id)
        return core.state_view(db, volume_id, branch,
                               branch["head_revision"] if revision is None else revision)


# ---------------------------------------------------------------- 决定

@app.post("/volumes/{volume_id}/branches/{branch_id}/decisions", status_code=201)
def decide(volume_id: str, branch_id: str, value: DecisionIn):
    payload = value.model_dump()
    with write_txn() as db:
        hit = core.idem_lookup(db, value.idempotency_key, "decide", payload)
        if hit:
            return JSONResponse(hit["body"], status_code=hit["status_code"])
        core.ensure_main(db, volume_id)
        branch = core.require_branch(db, volume_id, branch_id)
        decision = core.decide(db, volume_id, branch, value.group_id, value.action,
                               value.candidate_id, value.rationale, value.actor)
        core.idem_store(db, value.idempotency_key, "decide", payload, 201, decision)
        return decision


@app.get("/volumes/{volume_id}/groups/{group_id}/lineage")
def group_lineage(volume_id: str, group_id: str):
    """谱系：该异文组全部分支上的决定链及其来源。"""
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM decisions WHERE volume_id=? AND group_id=?"
            " ORDER BY created_at, decision_id", (volume_id, group_id)).fetchall()
        if not rows and not db.execute(
                "SELECT 1 FROM variant_groups WHERE volume_id=? AND group_id=?",
                (volume_id, group_id)).fetchone():
            raise HTTPException(404, "异文组不存在")
        return {"group_id": group_id,
                "decisions": [core.decision_view(row) for row in rows]}


# ---------------------------------------------------------------- 合并提案

@app.post("/volumes/{volume_id}/proposals", status_code=201)
def create_proposal(volume_id: str, value: ProposalCreate):
    payload = value.model_dump()
    with write_txn() as db:
        hit = core.idem_lookup(db, value.idempotency_key, "create_proposal", payload)
        if hit:
            return JSONResponse(hit["body"], status_code=hit["status_code"])
        core.ensure_main(db, volume_id)
        proposal = core.create_proposal(db, volume_id, value.source_branch,
                                        value.target_branch, value.actor)
        core.idem_store(db, value.idempotency_key, "create_proposal", payload, 201, proposal)
        return proposal


@app.get("/volumes/{volume_id}/proposals")
def list_proposals(volume_id: str):
    with connect() as db:
        return [core.proposal_view(row) for row in db.execute(
            "SELECT * FROM proposals WHERE volume_id=? ORDER BY created_at,proposal_id",
            (volume_id,))]


@app.get("/volumes/{volume_id}/proposals/{proposal_id}")
def get_proposal(volume_id: str, proposal_id: str):
    with connect() as db:
        return core.proposal_view(core.require_proposal(db, volume_id, proposal_id), db)


@app.post("/volumes/{volume_id}/proposals/{proposal_id}/resolutions", status_code=201)
def resolve(volume_id: str, proposal_id: str, value: ResolutionIn):
    with write_txn() as db:
        return core.resolve_conflict(db, volume_id, proposal_id, value.group_id,
                                     value.resolution, value.rationale, value.actor)


@app.post("/volumes/{volume_id}/proposals/{proposal_id}/merge", status_code=200)
def merge(volume_id: str, proposal_id: str, value: MergeIn):
    payload = value.model_dump()
    with write_txn() as db:
        # 幂等检查放在写事务内：并发同 key 请求在锁释放后能读到首次结果。
        hit = core.idem_lookup(db, value.idempotency_key, "merge", payload)
        if hit:
            return JSONResponse(hit["body"], status_code=hit["status_code"])
        result = core.merge_proposal(db, volume_id, proposal_id, value.actor)
        core.idem_store(db, value.idempotency_key, "merge", payload, 200, result)
        return result


# ---------------------------------------------------------------- 权限与事件流

@app.post("/volumes/{volume_id}/permissions", status_code=201)
def grant(volume_id: str, value: PermissionIn):
    with write_txn() as db:
        core.ensure_main(db, volume_id)
        core.grant_permission(db, volume_id, value.user_id, value.permission)
        return {"volume_id": volume_id, "user_id": value.user_id,
                "permission": value.permission}


@app.get("/volumes/{volume_id}/events")
def list_events(volume_id: str, after: int = 0, branch_id: str | None = None):
    sql = "SELECT * FROM events WHERE volume_id=? AND event_id>?"
    params: list = [volume_id, after]
    if branch_id:
        sql += " AND branch_id=?"
        params.append(branch_id)
    sql += " ORDER BY event_id"
    with connect() as db:
        rows = db.execute(sql, params).fetchall()
        cursor = db.execute(
            "SELECT COALESCE(MAX(event_id),0) AS m FROM events WHERE volume_id=?",
            (volume_id,)).fetchone()["m"]
        return {
            "events": [{
                "event_id": row["event_id"], "branch_id": row["branch_id"],
                "branch_revision": row["branch_revision"], "type": row["type"],
                "payload": json.loads(row["payload_json"]), "actor": row["actor"],
                "created_at": row["created_at"],
            } for row in rows],
            "next_cursor": cursor,
        }
