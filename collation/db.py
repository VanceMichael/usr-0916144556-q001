import os
import sqlite3
from pathlib import Path


def path() -> Path:
    return Path(os.getenv("COLLATION_DB_PATH", "data/collation.sqlite3"))


def connect() -> sqlite3.Connection:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target)
    connection.row_factory = sqlite3.Row
    return connection


def migrate() -> None:
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS submissions(
          submission_id TEXT PRIMARY KEY, volume_id TEXT NOT NULL,
          page INTEGER NOT NULL, base_revision INTEGER NOT NULL,
          segments_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """)
