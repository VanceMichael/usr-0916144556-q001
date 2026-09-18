import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def path() -> Path:
    return Path(os.getenv("COLLATION_DB_PATH", "data/collation.sqlite3"))


def connect() -> sqlite3.Connection:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    # WAL + busy timeout 让并发写事务在 BEGIN IMMEDIATE 上排队而不是立即报错。
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# 迁移按版本号追加，全部使用 IF NOT EXISTS / INSERT OR IGNORE，可重复执行。
_MIGRATIONS = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS submissions(
          submission_id TEXT PRIMARY KEY, volume_id TEXT NOT NULL,
          page INTEGER NOT NULL, base_revision INTEGER NOT NULL,
          segments_json TEXT NOT NULL, created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS volumes(
          volume_id TEXT PRIMARY KEY,
          head_revision INTEGER NOT NULL DEFAULT 0,
          event_cursor INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        -- 卷册修订谱系：每条 revision 指认父 revision，形成可回看的谱系链。
        CREATE TABLE IF NOT EXISTS revisions(
          volume_id TEXT NOT NULL,
          revision INTEGER NOT NULL,
          parent_revision INTEGER NOT NULL,
          origin TEXT NOT NULL,
          created_by TEXT,
          detail_json TEXT,
          created_at TEXT NOT NULL,
          PRIMARY KEY(volume_id, revision)
        );

        -- 每个 revision 的异文组全量快照（候选及其原始异文依据），旧 revision 永不被改写。
        CREATE TABLE IF NOT EXISTS variant_groups(
          volume_id TEXT NOT NULL,
          revision INTEGER NOT NULL,
          group_id TEXT NOT NULL,
          candidates_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY(volume_id, revision, group_id)
        );

        -- 每个 revision 上的决定快照（accept/reject/defer）。
        CREATE TABLE IF NOT EXISTS revision_decisions(
          volume_id TEXT NOT NULL,
          revision INTEGER NOT NULL,
          group_id TEXT NOT NULL,
          decision TEXT NOT NULL,
          candidate_id TEXT,
          evidence_json TEXT,
          decided_by TEXT,
          decided_at TEXT NOT NULL,
          PRIMARY KEY(volume_id, revision, group_id)
        );

        -- 审校分支：从明确的卷册 revision 建立。
        CREATE TABLE IF NOT EXISTS branches(
          branch_id TEXT PRIMARY KEY,
          volume_id TEXT NOT NULL,
          base_revision INTEGER NOT NULL,
          created_by TEXT,
          created_at TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'open',
          -- 源分支水位：分支上最后一条决定事件的序号，严格单调递增。
          decision_cursor INTEGER NOT NULL DEFAULT 0,
          merged_revision INTEGER
        );

        -- 分支决定只落在本分支；evidence_json 在决定时从基线快照复制，长期保留原始异文依据。
        CREATE TABLE IF NOT EXISTS branch_decisions(
          branch_id TEXT NOT NULL,
          group_id TEXT NOT NULL,
          decision TEXT NOT NULL,
          candidate_id TEXT,
          evidence_json TEXT,
          rationale TEXT,
          decided_by TEXT NOT NULL,
          decided_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          event_seq INTEGER,
          PRIMARY KEY(branch_id, group_id)
        );

        -- 追加式事件日志（决定、谱系、提案、裁决），seq 即事件游标。
        CREATE TABLE IF NOT EXISTS events(
          seq INTEGER PRIMARY KEY AUTOINCREMENT,
          volume_id TEXT NOT NULL,
          branch_id TEXT,
          kind TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );

        -- 审计事件，与业务事件共用同一事务落盘。
        CREATE TABLE IF NOT EXISTS audit_log(
          audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
          volume_id TEXT NOT NULL,
          branch_id TEXT,
          action TEXT NOT NULL,
          actor TEXT,
          detail_json TEXT,
          event_seq INTEGER,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS actors(
          actor_id TEXT PRIMARY KEY,
          permissions_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS merge_proposals(
          proposal_id TEXT PRIMARY KEY,
          volume_id TEXT NOT NULL,
          source_branch TEXT NOT NULL,
          -- 提交提案时固定的源/目标水位。
          target_watermark INTEGER NOT NULL,
          source_watermark INTEGER NOT NULL,
          preview_json TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'open',
          created_by TEXT,
          created_at TEXT NOT NULL,
          resolved_by TEXT,
          resolved_at TEXT,
          merge_revision INTEGER
        );

        -- 冲突项的逐项裁决记录。
        CREATE TABLE IF NOT EXISTS proposal_conflicts(
          proposal_id TEXT NOT NULL,
          group_id TEXT NOT NULL,
          decision TEXT NOT NULL,
          candidate_id TEXT,
          resolved_by TEXT NOT NULL,
          resolved_at TEXT NOT NULL,
          event_seq INTEGER NOT NULL,
          PRIMARY KEY(proposal_id, group_id)
        );

        -- 幂等键：相同 (actor, key) 的重试返回首次状态码与响应体。
        CREATE TABLE IF NOT EXISTS idempotency_keys(
          actor_id TEXT NOT NULL,
          idempotency_key TEXT NOT NULL,
          request_hash TEXT NOT NULL,
          status_code INTEGER NOT NULL,
          response_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY(actor_id, idempotency_key)
        );

        CREATE INDEX IF NOT EXISTS idx_events_volume ON events(volume_id, seq);
        CREATE INDEX IF NOT EXISTS idx_audit_volume ON audit_log(volume_id, audit_id);
        """,
    ),
]


def migrate() -> None:
    with connect() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version, ddl in _MIGRATIONS:
            applied = db.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?", (version,)
            ).fetchone()
            if applied:
                continue
            # DDL 全部为 IF NOT EXISTS / INSERT OR IGNORE 的幂等语句；
            # executescript 会先提交当前事务，故版本号在脚本成功后单独登记。
            # 若中途崩溃，重跑迁移时 DDL 全部成为空操作后补登版本号。
            db.executescript(ddl)
            db.execute(
                "INSERT INTO schema_migrations VALUES(?,?)", (version, now())
            )


class read_connection:
    """只读连接上下文，退出时关闭。"""

    def __init__(self):
        self.db = connect()

    def __enter__(self) -> sqlite3.Connection:
        return self.db

    def __exit__(self, exc_type, exc, tb) -> None:
        self.db.close()


class write_connection:
    """写连接 + 显式 BEGIN IMMEDIATE 事务，退出时提交/回滚并关闭连接。

    决定、谱系、审计事件与事件游标因此在同一 SQLite 事务内落盘。
    """

    def __init__(self):
        self.db = connect()

    def __enter__(self) -> sqlite3.Connection:
        self.db.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.db.execute("COMMIT")
            else:
                self.db.execute("ROLLBACK")
        finally:
            self.db.close()


def append_event(
    db: sqlite3.Connection,
    volume_id: str,
    kind: str,
    payload: dict,
    branch_id: str | None = None,
) -> int:
    """在当前事务内追加业务事件并返回事件序号（游标值）。"""
    cur = db.execute(
        "INSERT INTO events(volume_id, branch_id, kind, payload_json, created_at) VALUES(?,?,?,?,?)",
        (volume_id, branch_id, kind, json_dumps(payload), now()),
    )
    return int(cur.lastrowid)


def append_audit(
    db: sqlite3.Connection,
    volume_id: str,
    action: str,
    actor: str | None,
    detail: dict,
    event_seq: int | None,
    branch_id: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO audit_log(volume_id, branch_id, action, actor, detail_json, event_seq, created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (volume_id, branch_id, action, actor, json_dumps(detail), event_seq, now()),
    )


def json_dumps(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)
