"""SQLite 持久化层。

批次（batch）模型
-----------------
每次成功提交的一次上传生成一个新批次，并在同一事务内把旧批次置为
非当前。重报处理失败（任何异常）时事务整体回滚，旧批次保持原状，
满足“地图和核查结果一起生效，重报失败保留原状”。

结果沿用
--------
piece_results 以分片指纹为主键保存历史核定结论。新批次中指纹相同的
分片直接复用旧结论（status/adopted_class/source='reused'），不再
重读片内实测点；新分片才参与本次点核定。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

from .geometry import (
    ADOPTED_STATUSES,
    Piece,
    STATUS_PENDING,
    SurveyPoint,
)
from .wktutil import area_of, canonical_wkt, piece_fingerprint

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    map1_filename TEXT NOT NULL,
    map2_filename TEXT NOT NULL,
    points_filename TEXT NOT NULL,
    n_pieces INTEGER NOT NULL,
    n_points INTEGER NOT NULL,
    n_ingest INTEGER NOT NULL,
    n_pending INTEGER NOT NULL,
    stats_json TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS pieces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    piece_code TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    class_1 TEXT NOT NULL,
    class_2 TEXT NOT NULL,
    status TEXT NOT NULL,
    adopted_class TEXT,
    source TEXT NOT NULL,
    area_m2 REAL NOT NULL,
    wkt TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pieces_batch ON pieces(batch_id);
CREATE INDEX IF NOT EXISTS idx_pieces_status ON pieces(batch_id, status);

CREATE TABLE IF NOT EXISTS survey_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    point_code TEXT NOT NULL,
    x REAL NOT NULL,
    y REAL NOT NULL,
    land_class TEXT NOT NULL,
    placement TEXT NOT NULL,
    piece_code TEXT
);
CREATE INDEX IF NOT EXISTS idx_points_batch ON survey_points(batch_id);
CREATE INDEX IF NOT EXISTS idx_points_piece ON survey_points(batch_id, piece_code);

CREATE TABLE IF NOT EXISTS piece_results (
    fingerprint TEXT PRIMARY KEY,
    class_1 TEXT NOT NULL,
    class_2 TEXT NOT NULL,
    status TEXT NOT NULL,
    adopted_class TEXT,
    first_batch_id INTEGER,
    last_seen_batch_id INTEGER,
    updated_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def get_current_batch_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT id FROM batches WHERE is_current = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


def lookup_results(conn: sqlite3.Connection, fingerprints: list[str]) -> dict[str, dict]:
    """按指纹批量取回历史核定结论。"""
    if not fingerprints:
        return {}
    out: dict[str, dict] = {}
    # 分批以避免 SQL 变量上限
    chunk = 500
    for start in range(0, len(fingerprints), chunk):
        ids = fingerprints[start:start + chunk]
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT * FROM piece_results WHERE fingerprint IN ({placeholders})",
            ids,
        ).fetchall()
        for r in rows:
            out[r["fingerprint"]] = dict(r)
    return out


def apply_reuse(pieces: list[Piece], prior: dict[str, dict]) -> int:
    """把可沿用的历史结论写回分片对象，返回沿用数量。"""
    reused = 0
    for piece in pieces:
        fp = piece_fingerprint(piece.class_1, piece.class_2, piece.geom)
        piece.fingerprint = fp  # type: ignore[attr-defined]
        old = prior.get(fp)
        if old is not None:
            piece.status = old["status"]
            piece.adopted_class = old["adopted_class"]
            piece.source = "reused"
            reused += 1
    return reused


def save_batch(
    conn: sqlite3.Connection,
    *,
    filenames: tuple[str, str, str],
    pieces: list[Piece],
    points: list[SurveyPoint],
    stats: dict,
) -> int:
    """在单个事务内落库新批次并翻转 current；失败由调用方回滚。

    注意：调用方应先完成计算（含 apply_reuse / adjudicate），本函数
    只负责持久化，保证“地图和核查结果一起生效”。
    """
    cur = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM batches")
    new_batch_id = cur.fetchone()["next_id"]
    ts = now_iso()
    n_ingest = sum(1 for p in pieces if p.status in ADOPTED_STATUSES)
    n_pending = sum(1 for p in pieces if p.status == STATUS_PENDING)

    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE batches SET is_current = 0 WHERE is_current = 1")
        conn.execute(
            """INSERT INTO batches
               (id, created_at, map1_filename, map2_filename, points_filename,
                n_pieces, n_points, n_ingest, n_pending, stats_json, is_current)
               VALUES (?,?,?,?,?,?,?,?,?,?,1)""",
            (
                new_batch_id, ts, filenames[0], filenames[1], filenames[2],
                len(pieces), len(points), n_ingest, n_pending,
                json.dumps(stats, ensure_ascii=False),
            ),
        )
        conn.executemany(
            """INSERT INTO pieces
               (batch_id, piece_code, fingerprint, class_1, class_2,
                status, adopted_class, source, area_m2, wkt)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    new_batch_id, p.piece_id, p.fingerprint, p.class_1, p.class_2,
                    p.status, p.adopted_class, p.source, area_of(p.geom),
                    canonical_wkt(p.geom),
                )
                for p in pieces
            ],
        )
        conn.executemany(
            """INSERT INTO survey_points
               (batch_id, point_code, x, y, land_class, placement, piece_code)
               VALUES (?,?,?,?,?,?,?)""",
            [
                (
                    new_batch_id, pt.point_id, pt.x, pt.y, pt.land_class,
                    pt.placement, pt.piece_id,
                )
                for pt in points
            ],
        )
        # 指纹结论表：存在则更新最近批次，不存在则插入
        for p in pieces:
            conn.execute(
                """INSERT INTO piece_results
                   (fingerprint, class_1, class_2, status, adopted_class,
                    first_batch_id, last_seen_batch_id, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(fingerprint) DO UPDATE SET
                       status=excluded.status,
                       adopted_class=excluded.adopted_class,
                       last_seen_batch_id=excluded.last_seen_batch_id,
                       updated_at=excluded.updated_at""",
                (
                    p.fingerprint, p.class_1, p.class_2, p.status,
                    p.adopted_class, new_batch_id, new_batch_id, ts,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return new_batch_id


# --------------------------------------------------------------------------
# 查询辅助（网页 / 下载）
# --------------------------------------------------------------------------

def list_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM batches ORDER BY id DESC"
    ).fetchall()


def get_batch(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM batches WHERE id = ?", (batch_id,)
    ).fetchone()


def list_pieces(conn: sqlite3.Connection, batch_id: int,
                status: str | None = None) -> list[sqlite3.Row]:
    if status:
        return conn.execute(
            "SELECT * FROM pieces WHERE batch_id=? AND status=? ORDER BY piece_code",
            (batch_id, status),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM pieces WHERE batch_id=? ORDER BY piece_code", (batch_id,)
    ).fetchall()


def list_points(conn: sqlite3.Connection, batch_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM survey_points WHERE batch_id=? ORDER BY point_code",
        (batch_id,),
    ).fetchall()


def point_counts_by_piece(conn: sqlite3.Connection, batch_id: int) -> dict[str, int]:
    rows = conn.execute(
        """SELECT piece_code, COUNT(*) AS n FROM survey_points
           WHERE batch_id=? AND placement='interior' GROUP BY piece_code""",
        (batch_id,),
    ).fetchall()
    return {r["piece_code"]: r["n"] for r in rows}
