"""SQLite 持久层：危险区、生效时段、轨迹点。

- 所有写操作在单个事务中完成，批量失败不写入部分数据。
- 重启后数据保留（DZ_DB_PATH 指向持久文件）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .config import Settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS zones (
    zone_id      TEXT PRIMARY KEY,
    exterior     TEXT NOT NULL,          -- GeoJSON-like [[x,y],...]（闭合）
    holes        TEXT NOT NULL,          -- [[[x,y],...], ...]（各环闭合）
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS zone_windows (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id      TEXT NOT NULL REFERENCES zones(zone_id),
    start_epoch  REAL NOT NULL,
    end_epoch    REAL NOT NULL,
    start_iso    TEXT NOT NULL,
    end_iso      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_zone_windows_zone ON zone_windows(zone_id);

CREATE TABLE IF NOT EXISTS points (
    trip_id    TEXT NOT NULL,
    point_id   TEXT NOT NULL,
    ts_epoch   REAL NOT NULL,
    x          REAL NOT NULL,
    y          REAL NOT NULL,
    PRIMARY KEY (trip_id, point_id)
);
-- 同车次同刻必须是同一个点：部分索引防止 (trip, ts) 重复
CREATE UNIQUE INDEX IF NOT EXISTS idx_points_trip_ts
    ON points(trip_id, ts_epoch);
CREATE INDEX IF NOT EXISTS idx_points_trip ON points(trip_id, ts_epoch);

-- 全局修订号：单行计数器。每次区域登记/同 ID 覆盖原子地 +1。
CREATE TABLE IF NOT EXISTS revision_meta (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    head     INTEGER NOT NULL DEFAULT 0
);

-- 各区不可变历史版本快照。每个登记/覆盖版本一行，revision 即全局修订号。
CREATE TABLE IF NOT EXISTS zone_revisions (
    revision     INTEGER PRIMARY KEY,
    zone_id      TEXT NOT NULL,
    exterior     TEXT NOT NULL,
    holes        TEXT NOT NULL,
    windows      TEXT NOT NULL,          -- [{"start_epoch","end_epoch","start_iso","end_iso"}]
    created_at   TEXT NOT NULL,
    recorded_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_zone_revisions_zone_rev
    ON zone_revisions(zone_id, revision);
"""


def _connect(settings: Settings) -> sqlite3.Connection:
    conn = sqlite3.connect(
        settings.db_path,
        timeout=30,
        isolation_level=None,  # 显式事务
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = _connect(settings)
        # SQLite 单连接在多线程共享时，并发事务会互相交错/打断；
        # 用可重入锁串行化所有事务，保证并发更新与查询互不读到半次提交。
        self._tx_lock = threading.RLock()
        # 同线程只读事务可重入（穿越查询外层包整段、内层取历史版本）
        self._tx_local = threading.local()
        self.init_schema()
        self.migrate_legacy_baseline()

    def init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn
        with self._tx_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    @contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        """只读事务：整段读取锁定同一快照，写者提交前/后都不会读到半次提交。

        可在同一线程内重入：嵌套层复用外层事务，仅由最外层提交。
        """
        conn = self._conn
        depth = getattr(self._tx_local, "depth", 0)
        with self._tx_lock:
            if depth == 0:
                conn.execute("BEGIN")
            self._tx_local.depth = depth + 1
            try:
                yield conn
                if depth == 0:
                    conn.execute("COMMIT")
            except Exception:
                if depth == 0:
                    conn.execute("ROLLBACK")
                raise
            finally:
                self._tx_local.depth = depth

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    # ----- 危险区 ----------------------------------------------------------

    def get_zone_row(self, zone_id: str) -> Optional[sqlite3.Row]:
        cur = self._conn.execute("SELECT * FROM zones WHERE zone_id = ?", (zone_id,))
        return cur.fetchone()

    def list_zone_rows(self) -> List[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM zones ORDER BY zone_id"))

    def get_windows(self, zone_id: str) -> List[Tuple[float, float]]:
        cur = self._conn.execute(
            "SELECT start_epoch, end_epoch FROM zone_windows WHERE zone_id = ? "
            "ORDER BY start_epoch",
            (zone_id,),
        )
        return [(row["start_epoch"], row["end_epoch"]) for row in cur.fetchall()]

    def insert_zone(
        self,
        conn: sqlite3.Connection,
        zone_id: str,
        exterior: List[List[float]],
        holes: List[List[List[float]]],
        windows: List[Dict[str, Any]],
        created_iso: str,
    ) -> None:
        conn.execute(
            "INSERT INTO zones(zone_id, exterior, holes, created_at) VALUES (?,?,?,?)",
            (zone_id, json.dumps(exterior), json.dumps(holes), created_iso),
        )
        conn.executemany(
            "INSERT INTO zone_windows(zone_id, start_epoch, end_epoch, start_iso, end_iso) "
            "VALUES (?,?,?,?,?)",
            [
                (
                    zone_id,
                    w["start_epoch"],
                    w["end_epoch"],
                    w["start_iso"],
                    w["end_iso"],
                )
                for w in windows
            ],
        )

    # ----- 修订号 / 不可变历史版本 ----------------------------------------

    def head_revision(self) -> int:
        cur = self._conn.execute("SELECT head FROM revision_meta WHERE id = 1")
        row = cur.fetchone()
        return int(row["head"]) if row is not None else 0

    def allocate_revision(self, conn: sqlite3.Connection) -> int:
        """在写事务内原子地分配下一个全局递增修订号。"""
        conn.execute(
            "INSERT INTO revision_meta(id, head) VALUES (1, 1) "
            "ON CONFLICT(id) DO UPDATE SET head = head + 1"
        )
        cur = conn.execute("SELECT head FROM revision_meta WHERE id = 1")
        return int(cur.fetchone()["head"])

    def insert_zone_revision(
        self,
        conn: sqlite3.Connection,
        revision: int,
        zone_id: str,
        exterior: List[List[float]],
        holes: List[List[List[float]]],
        windows: List[Dict[str, Any]],
        created_at: str,
        recorded_at: str,
    ) -> None:
        conn.execute(
            "INSERT INTO zone_revisions"
            "(revision, zone_id, exterior, holes, windows, created_at, recorded_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                revision,
                zone_id,
                json.dumps(exterior),
                json.dumps(holes),
                json.dumps(windows),
                created_at,
                recorded_at,
            ),
        )

    def get_zone_version(
        self, conn: sqlite3.Connection, zone_id: str, revision: int
    ) -> Optional[sqlite3.Row]:
        """取某区截至 revision 的最后一个不可变版本；尚未登记则返回 None。"""
        cur = conn.execute(
            "SELECT * FROM zone_revisions WHERE zone_id = ? AND revision <= ? "
            "ORDER BY revision DESC LIMIT 1",
            (zone_id, revision),
        )
        return cur.fetchone()

    def list_zone_versions(self, zone_id: str) -> List[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT revision, zone_id, recorded_at FROM zone_revisions "
                "WHERE zone_id = ? ORDER BY revision",
                (zone_id,),
            )
        )

    def migrate_legacy_baseline(self) -> int:
        """把旧库现存区域迁移为各自的基线历史版本（不推断迁移前历史）。

        仅在历史表为空、且存在尚未进入历史的区域时执行一次；按创建时刻、
        zone_id 顺序在单事务内分配递增修订号。新建库为空，结果为 0。
        """
        legacy = list(
            self._conn.execute(
                "SELECT z.zone_id, z.exterior, z.holes, z.created_at "
                "FROM zones z ORDER BY z.created_at, z.zone_id"
            )
        )
        if not legacy:
            return 0
        done_ids = {
            r["zone_id"]
            for r in self._conn.execute("SELECT DISTINCT zone_id FROM zone_revisions")
        }
        pending = [r for r in legacy if r["zone_id"] not in done_ids]
        if not pending:
            return 0

        migrated = 0
        with self.transaction() as conn:
            for zrow in pending:
                win_rows = list(
                    conn.execute(
                        "SELECT start_epoch, end_epoch, start_iso, end_iso "
                        "FROM zone_windows WHERE zone_id = ? ORDER BY start_epoch",
                        (zrow["zone_id"],),
                    )
                )
                windows = [
                    {
                        "start_epoch": w["start_epoch"],
                        "end_epoch": w["end_epoch"],
                        "start_iso": w["start_iso"],
                        "end_iso": w["end_iso"],
                    }
                    for w in win_rows
                ]
                revision = self.allocate_revision(conn)
                self.insert_zone_revision(
                    conn,
                    revision,
                    zrow["zone_id"],
                    json.loads(zrow["exterior"]),
                    json.loads(zrow["holes"]),
                    windows,
                    zrow["created_at"],
                    zrow["created_at"],
                )
                migrated += 1
        return migrated

    # ----- 轨迹点 ----------------------------------------------------------

    def get_point(self, trip_id: str, point_id: str) -> Optional[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM points WHERE trip_id = ? AND point_id = ?",
            (trip_id, point_id),
        )
        return cur.fetchone()

    def find_point_at_ts(self, trip_id: str, ts_epoch: float) -> Optional[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM points WHERE trip_id = ? AND ts_epoch = ?",
            (trip_id, ts_epoch),
        )
        return cur.fetchone()

    def list_trip_points(self, trip_id: str) -> List[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM points WHERE trip_id = ? ORDER BY ts_epoch, point_id",
                (trip_id,),
            )
        )

    def count_trip_points(self, trip_id: str) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) AS c FROM points WHERE trip_id = ?", (trip_id,)
        )
        return int(cur.fetchone()["c"])

    def insert_points(
        self, conn: sqlite3.Connection, rows: List[Tuple[str, str, float, float, float]]
    ) -> None:
        conn.executemany(
            "INSERT INTO points(trip_id, point_id, ts_epoch, x, y) VALUES (?,?,?,?,?)",
            rows,
        )
