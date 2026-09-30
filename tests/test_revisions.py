"""区域修订号与历史版本复现测试。"""
import threading

from tests.conftest import DAY_WINDOWS, SQUARE, register_square


def _points(points):
    return {"points": [{"point_id": pid, "ts": ts, "x": x, "y": y} for pid, ts, x, y in points]}


# 自西向东匀速横穿 0..100 方形：10:00:00 在 x=-50，10:01:20 在 x=150（80s 走 200m）
BASIC = [
    ("p1", "2026-09-30T10:00:00Z", -50, 50),
    ("p2", "2026-09-30T10:00:40Z", 50, 50),
    ("p3", "2026-09-30T10:01:20Z", 150, 50),
]


def test_register_returns_monotonic_revision(client):
    r1 = register_square(client, zone_id="z-a")
    assert r1.status_code == 201
    assert r1.json()["revision"] == 1
    r2 = register_square(client, zone_id="z-b")
    assert r2.json()["revision"] == 2
    # 同 ID 覆盖同样产生一个新的全局修订号
    r3 = register_square(client, zone_id="z-a")
    assert r3.json()["status"] == "updated"
    assert r3.json()["revision"] == 3
    cur = client.get("/api/v1/revisions/current").json()
    assert cur["revision"] == 3


def test_latest_path_has_no_revision_field(client):
    register_square(client, zone_id="z")
    client.post("/api/v1/trips/T/points", json=_points(BASIC))
    body = client.get("/api/v1/trips/T/crossings").json()
    assert "revision" not in body
    assert "revision" not in body["zones"][0]


def test_reproduces_geometry_at_revision(client):
    # 修订 1：大方形 0..100，横穿 40 秒
    register_square(client, zone_id="geo")
    client.post("/api/v1/trips/TG/points", json=_points(BASIC))
    assert client.get("/api/v1/trips/TG/crossings?zone_id=geo").json()["zones"][0][
        "total_duration_sec"
    ] == 40.0

    # 修订 2：把区域缩小为 40..60（仅 20m，横穿 8 秒）
    r2 = register_square(client, zone_id="geo", exterior=[[40, 0], [60, 0], [60, 100], [40, 100]])
    rev2 = r2.json()["revision"]

    # 最新配置：8 秒
    latest = client.get("/api/v1/trips/TG/crossings?zone_id=geo").json()["zones"][0]
    assert latest["total_duration_sec"] == 8.0

    # 按修订 1 复现：仍是旧几何，40 秒
    at1 = client.get("/api/v1/trips/TG/crossings?zone_id=geo&revision=1").json()
    assert at1["revision"] == 1
    z1 = at1["zones"][0]
    assert z1["revision"] == 1
    assert z1["total_duration_sec"] == 40.0

    # 修订 2 复现：新几何，8 秒
    at2 = client.get(f"/api/v1/trips/TG/crossings?zone_id=geo&revision={rev2}").json()
    assert at2["revision"] == rev2
    assert at2["zones"][0]["total_duration_sec"] == 8.0


def test_reproduces_windows_at_revision(client):
    # 修订 1：仅 10:00:20~10:00:50 生效（30 秒）
    register_square(
        client,
        zone_id="win",
        windows=[{"start": "2026-09-30T10:00:20Z", "end": "2026-09-30T10:00:50Z"}],
    )
    client.post("/api/v1/trips/TW/points", json=_points(BASIC))
    # 修订 2：改为全天生效（40 秒）
    register_square(client, zone_id="win", windows=DAY_WINDOWS)

    at1 = client.get("/api/v1/trips/TW/crossings?zone_id=win&revision=1").json()["zones"][0]
    assert at1["total_duration_sec"] == 30.0
    assert at1["segments"][0]["end"] == "2026-09-30T10:00:50Z"

    latest = client.get("/api/v1/trips/TW/crossings?zone_id=win").json()["zones"][0]
    assert latest["total_duration_sec"] == 40.0


def test_zone_not_yet_registered_excluded_at_revision(client):
    register_square(client, zone_id="early")  # rev 1
    register_square(client, zone_id="later")  # rev 2
    client.post("/api/v1/trips/TN/points", json=_points(BASIC))

    # 全量按修订 1：later 尚未登记，不参与核算
    at1 = client.get("/api/v1/trips/TN/crossings?revision=1").json()
    ids = {z["zone_id"] for z in at1["zones"]}
    assert ids == {"early"}

    # 指定一个当时尚未登记的 zone_id：空结果，不报错
    at1_later = client.get("/api/v1/trips/TN/crossings?zone_id=later&revision=1").json()
    assert at1_later["zones"] == []

    # 最新路径下不存在的 zone_id 仍维持原来的 400
    r = client.get("/api/v1/trips/TN/crossings?zone_id=nope")
    assert r.status_code == 400

    # 修订 2：两者都在
    at2 = client.get("/api/v1/trips/TN/crossings?revision=2").json()
    assert {z["zone_id"] for z in at2["zones"]} == {"early", "later"}


def test_revision_out_of_range_404(client):
    register_square(client, zone_id="z")
    r = client.get("/api/v1/trips/TX/crossings?revision=999")
    assert r.status_code == 404
    assert r.json()["error"] == "revision_not_found"
    assert "999" in r.json()["detail"]


def test_revision_empty_db_is_zero(client):
    assert client.get("/api/v1/revisions/current").json()["revision"] == 0
    r = client.get("/api/v1/trips/whatever/crossings?revision=1")
    assert r.status_code == 404
    assert r.json()["error"] == "revision_not_found"


def test_revision_must_be_positive(client):
    r = client.get("/api/v1/trips/T/crossings?revision=0")
    assert r.status_code == 422
    r = client.get("/api/v1/trips/T/crossings?revision=abc")
    assert r.status_code == 422


def test_version_snapshot_takes_last_version_up_to_revision(client):
    register_square(client, zone_id="v")  # rev 1
    register_square(client, zone_id="v", exterior=[[40, 0], [60, 0], [60, 100], [40, 100]])  # rev 2
    register_square(client, zone_id="v", exterior=[[10, 0], [90, 0], [90, 100], [10, 100]])  # rev 3
    client.post("/api/v1/trips/TV/points", json=_points(BASIC))

    # 修订 2 与 3 之间还有其它登记也不影响：取 v 截至该号的最后版本
    # rev2 宽度 20m -> 8s；rev3 宽度 80m -> 32s
    assert client.get("/api/v1/trips/TV/crossings?zone_id=v&revision=2").json()["zones"][0][
        "total_duration_sec"
    ] == 8.0
    assert client.get("/api/v1/trips/TV/crossings?zone_id=v&revision=3").json()["zones"][0][
        "total_duration_sec"
    ] == 32.0


def test_zone_revisions_history_endpoint(client):
    register_square(client, zone_id="hist")  # 1
    register_square(client, zone_id="other")  # 2
    register_square(client, zone_id="hist")  # 3

    r = client.get("/api/v1/zones/hist/revisions")
    assert r.status_code == 200
    body = r.json()
    assert [item["revision"] for item in body["revisions"]] == [1, 3]
    assert body["current_revision"] == 3
    for item in body["revisions"]:
        assert item["recorded_at"].endswith("Z")

    assert client.get("/api/v1/zones/missing/revisions").status_code == 404


def test_concurrent_registrations_get_unique_revisions(client):
    errors = []
    results = []
    lock = threading.Lock()

    def worker(start):
        try:
            for i in range(start, start + 5):
                r = register_square(client, zone_id=f"conc-{i}")
                with lock:
                    results.append(r.json()["revision"])
        except Exception as exc:  # pragma: no cover - 仅用于暴露并发问题
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(base,)) for base in (0, 10, 20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(results) == 15
    assert len(set(results)) == 15
    assert sorted(results) == list(range(1, 16))
    assert client.get("/api/v1/revisions/current").json()["revision"] == 15
