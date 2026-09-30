"""持久化测试：重新打开同一数据库文件后，历史记录仍可查询。"""
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone

from app.config import get_settings
from app.db import Database
from app.service import AuditService
from tests.conftest import SQUARE


def test_records_survive_database_reopen():
    settings = get_settings()
    path = settings.db_path
    assert os.path.exists(path)

    db1 = Database(settings)
    svc1 = AuditService(db1)
    svc1.register_zone(
        {
            "zone_id": "persist-z",
            "exterior": SQUARE,
            "holes": [],
            "windows": [{"start": "2026-09-30T00:00:00Z", "end": "2026-10-01T00:00:00Z"}],
        }
    )
    svc1.submit_points(
        "persist-trip",
        {
            "points": [
                {"point_id": "q1", "ts": "2026-09-30T10:00:00Z", "x": -50, "y": 50},
                {"point_id": "q2", "ts": "2026-09-30T10:00:40Z", "x": 50, "y": 50},
                {"point_id": "q3", "ts": "2026-09-30T10:01:20Z", "x": 150, "y": 50},
            ]
        },
    )
    db1.close()

    # 模拟进程重启：用同一文件新建连接
    db2 = Database(settings)
    svc2 = AuditService(db2)
    zone = svc2.get_zone("persist-z")
    assert zone is not None
    result = svc2.query_crossings("persist-trip", zone_id="persist-z")
    z = result["zones"][0]
    assert z["crossed"] is True
    assert z["total_duration_sec"] == 40.0
    db2.close()


def _build_legacy_db(path):
    """手工构造一份仅含旧表（无修订表）的危险区数据，模拟升级前的库。"""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE zones (
            zone_id TEXT PRIMARY KEY, exterior TEXT NOT NULL,
            holes TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE zone_windows (
            id INTEGER PRIMARY KEY AUTOINCREMENT, zone_id TEXT NOT NULL,
            start_epoch REAL NOT NULL, end_epoch REAL NOT NULL,
            start_iso TEXT NOT NULL, end_iso TEXT NOT NULL
        );
        CREATE TABLE points (
            trip_id TEXT NOT NULL, point_id TEXT NOT NULL,
            ts_epoch REAL NOT NULL, x REAL NOT NULL, y REAL NOT NULL,
            PRIMARY KEY (trip_id, point_id)
        );
        CREATE UNIQUE INDEX idx_points_trip_ts ON points(trip_id, ts_epoch);
        CREATE INDEX idx_points_trip ON points(trip_id, ts_epoch);
        """
    )
    created = "2026-09-01T00:00:00Z"
    conn.execute(
        "INSERT INTO zones(zone_id, exterior, holes, created_at) VALUES (?,?,?,?)",
        ("legacy-z", json.dumps(SQUARE + [SQUARE[0]]), json.dumps([]), created),
    )
    conn.execute(
        "INSERT INTO zone_windows(zone_id, start_epoch, end_epoch, start_iso, end_iso) "
        "VALUES (?,?,?,?,?)",
        (
            "legacy-z",
            datetime(2026, 9, 30, tzinfo=timezone.utc).timestamp(),
            datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp(),
            "2026-09-30T00:00:00Z",
            "2026-10-01T00:00:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO points(trip_id, point_id, ts_epoch, x, y) VALUES (?,?,?,?,?)",
        ("legacy-trip", "q1", datetime(2026, 9, 30, 10, 0, 0, tzinfo=timezone.utc).timestamp(), -50, 50),
    )
    conn.execute(
        "INSERT INTO points(trip_id, point_id, ts_epoch, x, y) VALUES (?,?,?,?,?)",
        ("legacy-trip", "q2", datetime(2026, 9, 30, 10, 0, 40, tzinfo=timezone.utc).timestamp(), 50, 50),
    )
    conn.execute(
        "INSERT INTO points(trip_id, point_id, ts_epoch, x, y) VALUES (?,?,?,?,?)",
        ("legacy-trip", "q3", datetime(2026, 9, 30, 10, 1, 20, tzinfo=timezone.utc).timestamp(), 150, 50),
    )
    conn.commit()
    conn.close()


def test_legacy_zones_migrate_as_baseline():
    tmp = tempfile.mkdtemp(prefix="dz-legacy-")
    path = os.path.join(tmp, "legacy.db")
    _build_legacy_db(path)

    # 第一次以新版本打开：自动建表 + 基线迁移
    settings = get_settings()
    from pathlib import Path

    settings.db_path = Path(path)
    db = Database(settings)
    svc = AuditService(db)

    assert svc.head_revision() == 1
    revs = svc.list_zone_revisions("legacy-z")
    assert revs == [{"revision": 1, "recorded_at": "2026-09-01T00:00:00Z"}]

    # 基线版本可按修订号复现，且与最新配置结果一致（40 秒）
    at1 = svc.query_crossings("legacy-trip", zone_id="legacy-z", revision=1)["zones"][0]
    assert at1["revision"] == 1
    assert at1["total_duration_sec"] == 40.0
    latest = svc.query_crossings("legacy-trip", zone_id="legacy-z")["zones"][0]
    assert latest["total_duration_sec"] == 40.0

    # 迁移幂等：再次打开不会重复迁移、修订号不跳
    db.close()
    db2 = Database(settings)
    svc2 = AuditService(db2)
    assert svc2.head_revision() == 1
    assert len(svc2.list_zone_revisions("legacy-z")) == 1

    # 迁移后的新登记从下一个修订号继续
    out = svc2.register_zone(
        {
            "zone_id": "new-z",
            "exterior": SQUARE,
            "holes": [],
            "windows": [{"start": "2026-09-30T00:00:00Z", "end": "2026-10-01T00:00:00Z"}],
        }
    )
    assert out["revision"] == 2
    db2.close()
