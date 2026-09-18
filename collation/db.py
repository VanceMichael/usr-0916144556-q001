import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

MAIN_BRANCH = "main"


def path() -> Path:
    return Path(os.getenv("COLLATION_DB_PATH", "data/collation.sqlite3"))


def connect() -> sqlite3.Connection:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # timeout 让并发写者在持锁期间等待而不是立刻报 database is locked，
    # 配合 BEGIN IMMEDIATE 保证同一时刻只有一个写事务推进。
    connection = sqlite3.connect(target, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def write_txn():
    """单个 SQLite 事务：决定、谱系、审计事件与事件游标在同一事务内落盘。

    BEGIN IMMEDIATE 在进入事务时即取得写锁，两个并发写事务只有一个能先
    推进，另一个在锁释放后重新检查水位，从而实现合并的互斥推进。
    """
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions(
  submission_id TEXT PRIMARY KEY, volume_id TEXT NOT NULL,
  page INTEGER NOT NULL, base_revision INTEGER NOT NULL,
  segments_json TEXT NOT NULL, created_at TEXT NOT NULL
);

-- 审校分支：main 为卷册主线，其余分支从明确的主线 revision 建立。
CREATE TABLE IF NOT EXISTS branches(
  volume_id TEXT NOT NULL, branch_id TEXT NOT NULL,
  name TEXT NOT NULL,
  base_branch TEXT, base_revision INTEGER,
  head_revision INTEGER NOT NULL DEFAULT 0,
  created_by TEXT, created_at TEXT NOT NULL,
  PRIMARY KEY(volume_id, branch_id)
);

-- 异文组与其候选（原始异文依据），挂在卷册主线上。
CREATE TABLE IF NOT EXISTS variant_groups(
  group_id TEXT PRIMARY KEY, volume_id TEXT NOT NULL,
  page INTEGER NOT NULL, segment_key TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates(
  candidate_id TEXT PRIMARY KEY, group_id TEXT NOT NULL,
  text TEXT NOT NULL, evidence_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(group_id, text)
);

-- 决定链：accept/reject/defer 只落在所属分支上；
-- origin_decision_id / supersedes_decision_id / proposal_id 构成谱系。
CREATE TABLE IF NOT EXISTS decisions(
  decision_id TEXT PRIMARY KEY,
  volume_id TEXT NOT NULL, branch_id TEXT NOT NULL,
  group_id TEXT NOT NULL, action TEXT NOT NULL,
  candidate_id TEXT, rationale TEXT NOT NULL DEFAULT '',
  actor TEXT NOT NULL, branch_revision INTEGER NOT NULL,
  origin_decision_id TEXT, supersedes_decision_id TEXT,
  proposal_id TEXT, created_at TEXT NOT NULL
);

-- 每个 (分支, 异文组) 的当前决定，供合并预览快速比对。
CREATE TABLE IF NOT EXISTS current_decisions(
  volume_id TEXT NOT NULL, branch_id TEXT NOT NULL,
  group_id TEXT NOT NULL, decision_id TEXT NOT NULL,
  PRIMARY KEY(volume_id, branch_id, group_id)
);

-- 合并提案：创建时固定源分支水位与目标分支水位。
CREATE TABLE IF NOT EXISTS proposals(
  proposal_id TEXT PRIMARY KEY, volume_id TEXT NOT NULL,
  source_branch TEXT NOT NULL, target_branch TEXT NOT NULL,
  source_watermark INTEGER NOT NULL, target_watermark INTEGER NOT NULL,
  preview_json TEXT NOT NULL, status TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at TEXT NOT NULL,
  merged_at TEXT, merge_revision INTEGER
);

-- 冲突项逐项裁决。
CREATE TABLE IF NOT EXISTS resolutions(
  proposal_id TEXT NOT NULL, group_id TEXT NOT NULL,
  resolution TEXT NOT NULL, rationale TEXT,
  actor TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(proposal_id, group_id)
);

CREATE TABLE IF NOT EXISTS permissions(
  volume_id TEXT NOT NULL, user_id TEXT NOT NULL,
  permission TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(volume_id, user_id, permission)
);

-- 审计事件流：状态可按 (branch, revision) 回放还原。
CREATE TABLE IF NOT EXISTS events(
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  volume_id TEXT NOT NULL, branch_id TEXT NOT NULL,
  branch_revision INTEGER NOT NULL, type TEXT NOT NULL,
  payload_json TEXT NOT NULL, actor TEXT,
  created_at TEXT NOT NULL
);

-- 事件游标：与事件、决定在同一事务内推进。
CREATE TABLE IF NOT EXISTS cursors(
  volume_id TEXT NOT NULL, branch_id TEXT NOT NULL,
  head_revision INTEGER NOT NULL, last_event_id INTEGER NOT NULL,
  PRIMARY KEY(volume_id, branch_id)
);

-- 幂等键：相同 key 重试返回首次结果。
CREATE TABLE IF NOT EXISTS idempotency(
  key TEXT PRIMARY KEY, endpoint TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  status_code INTEGER NOT NULL, response_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def migrate() -> None:
    connection = connect()
    try:
        connection.executescript(SCHEMA)
        connection.commit()
    finally:
        connection.close()
