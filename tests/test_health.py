"""Reconnects, health numbers, bump detection, event history and the walk test."""

import json

import numpy as np
import pytest

from walkway.geometry import FloorFit, Mount
from walkway.health import BumpCheck, EventLog, Health, ResilientSource, WalkTest
from walkway.tracks import Track
from walkway.zones import Zone


class FlakyCamera:
    """Gives `good` frames, then fails; the first `fail_opens` opens fail too."""

    opens = 0

    def __init__(self, good=3, fail_opens=0):
        FlakyCamera.opens += 1
        if FlakyCamera.opens <= fail_opens:
            raise RuntimeError("no camera")
        self.intrinsics = "intr"
        self.good = good
        self.closed = False

    def frames(self):
        for i in range(self.good):
            yield np.full((2, 2), FlakyCamera.opens * 100 + i, dtype=np.uint16)
        raise RuntimeError("X_LINK_ERROR")

    def close(self):
        self.closed = True


def test_reconnects_after_a_drop_and_reports_status():
    FlakyCamera.opens = 0
    status, sleeps = [], []
    src = ResilientSource(lambda: FlakyCamera(good=2, fail_opens=2), on_status=status.append, sleep=sleeps.append)
    assert sleeps == [1.0, 2.0]  # two failed opens, backing off
    frames = src.frames()
    got = [int(next(frames)[0, 0]) for _ in range(4)]
    assert got == [300, 301, 400, 401]  # third open worked, dropped, fourth open continues
    assert src.reconnects == 1
    assert status == [True, False, True]  # starts offline, so the failed opens are silent


def test_backoff_is_capped():
    FlakyCamera.opens = 0
    sleeps = []
    ResilientSource(lambda: FlakyCamera(fail_opens=8), sleep=sleeps.append, max_backoff=30)
    assert sleeps == [1, 2, 4, 8, 16, 30, 30, 30]


def test_health_measures_fps_and_delay():
    h = Health()
    for i in range(50):
        t = i / 15
        h.tick(t, t + 0.04)
    assert h.fps == pytest.approx(15, rel=0.02)
    assert h.delay_ms == pytest.approx(40, rel=0.02)


def fit(height, tilt, roll=0.0):
    return FloorFit(height=height, tilt_deg=tilt, roll_deg=roll, inlier_share=0.8)


def test_bump_check_needs_several_disagreeing_checks():
    m = Mount(height=2.7, tilt_deg=50)
    b = BumpCheck(confirm=3)
    assert b.check(fit(2.7, 50.2), m, 0) is None
    assert b.check(fit(2.7, 56), m, 60) is None  # one odd reading: ignored
    assert b.check(fit(2.7, 50.1), m, 120) is None
    assert b.check(fit(2.62, 56), m, 180) is None
    assert b.check(fit(2.62, 56), m, 240) is None
    assert b.check(fit(2.62, 56), m, 300) is True
    assert "Auto-level" in b.detail
    assert b.check(fit(2.7, 50), m, 360) is False  # put back


def test_bump_check_is_due_every_interval():
    b = BumpCheck(every_seconds=60)
    assert b.due(0)
    b.check(fit(2.7, 0), Mount(height=2.7), 0)
    assert not b.due(30) and b.due(61)


def test_event_log_keeps_recent_and_writes_file(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(path, keep=3)
    for i in range(5):
        log.add("walkway", zone="left_walkway", on=bool(i % 2))
    assert len(log.last()) == 3
    lines = path.read_text().splitlines()
    assert len(lines) == 5 and json.loads(lines[0])["zone"] == "left_walkway"
    assert "image" not in path.read_text()


def test_event_log_rotates(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(path, max_bytes=200)
    for _ in range(20):
        log.add("room", occupied=True)
    assert (tmp_path / "events.jsonl.1").exists()
    assert path.stat().st_size < 400


def walker(x, y):
    return Track(1, x, y, standing=True, hits=5)


def test_walk_test_suggests_a_threshold_between_noise_and_walking():
    z = Zone("left_walkway", -2.4, -1.6, -2, 2, min_points=100)
    wt = WalkTest()
    wt.start(["left_walkway"], 0)
    for s in (40, 55, 60, 30, 50):  # seated people next to it
        wt.feed([z], {"left_walkway": s}, [], {})
    for s in (210, 240, 260, 230, 250, 220):  # walking through
        wt.feed([z], {"left_walkway": s}, [walker(-2.0, 0.0)], {"left_walkway": s == 210})
    (row,) = wt.report([z], 30)["zones"]
    assert row["idle_max"] == 60 and row["triggers"] == 1
    assert 60 < row["suggested"] < row["walk_low"]


def test_walk_test_warns_when_walking_and_sitting_look_alike():
    z = Zone("left_walkway", -2.4, -1.6, -2, 2)
    wt = WalkTest()
    wt.start(["left_walkway"], 0)
    for s in (120, 140):
        wt.feed([z], {"left_walkway": s}, [], {})
    for s in (150, 160):
        wt.feed([z], {"left_walkway": s}, [walker(-2.0, 0.0)], {})
    (row,) = wt.report([z], 10)["zones"]
    assert "suggested" not in row and "too similar" in row["advice"]


def test_walk_test_on_simulated_room_gives_usable_advice():
    from test_tracking import make, walk_out_script

    src, pipe = make()
    wt = WalkTest()
    wt.start([z.name for z in pipe.zones.zones], 0)
    others = [(0.4, -0.9, 1.25), (-1.2, -0.9, 1.25)]
    for i, people in enumerate(walk_out_script() + [others] * 5):
        src.people = people + others
        r = pipe.process(src.render(), i / 15)
        wt.feed(pipe.zones.zones, pipe.zones.counts(r.points, r.weights), r.tracks, r.zone_changes, i / 15)
    rows = {row["zone"]: row for row in wt.report(pipe.zones.zones, 3)["zones"]}
    assert rows["left_walkway"]["triggers"] == 1 and "suggested" in rows["left_walkway"]
    assert "Nobody walked" in rows["right_walkway"]["advice"]
