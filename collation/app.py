import json
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from .db import connect, migrate

app = FastAPI(title="古籍异文汇校服务")


class Submission(BaseModel):
    submission_id: str
    volume_id: str
    page: int
    base_revision: int
    segments: list[dict]


@app.on_event("startup")
def startup() -> None:
    migrate()


@app.post("/submissions", status_code=201)
def submit(value: Submission):
    with connect() as db:
        old = db.execute("SELECT * FROM submissions WHERE submission_id=?", (value.submission_id,)).fetchone()
        body = json.dumps(value.segments, ensure_ascii=False, sort_keys=True)
        if old:
            if old["segments_json"] != body:
                raise HTTPException(409, "submission_id 已被其他内容使用")
            return dict(old)
        db.execute("INSERT INTO submissions VALUES(?,?,?,?,?,?)", (value.submission_id, value.volume_id, value.page, value.base_revision, body, datetime.now(timezone.utc).isoformat()))
    return value.model_dump()


@app.get("/volumes/{volume_id}/submissions")
def list_submissions(volume_id: str):
    with connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM submissions WHERE volume_id=? ORDER BY page,submission_id", (volume_id,))]
