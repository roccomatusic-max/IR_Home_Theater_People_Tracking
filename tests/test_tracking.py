"""Whole-room tracking, pre-arming and accuracy under noise, on simulated rooms."""

import numpy as np
import pytest

from walkway.geometry import Mount
from walkway.people import EmptyRoom, PeopleFinder, Person
from walkway.pipeline import Pipeline
from walkway.sources import SimDepthSource
from walkway.tracks import PeopleTracker
from walkway.zones import RoomPresence, Zone, ZoneTracker

OVERHEAD = Mount(height=2.7)
REAR = Mount(y=-4.5, height=2.7, tilt_deg=50)
SEAT_XS = (-1.2, -0.4, 0.4, 1.2)
ROW_YS = (0.6, -0.9)


def seats():
    """Two rows of four seats: cushion 0.5 m high, back 1.0 m high behind it."""
    out = []
    for y in ROW_YS:
        for x in SEAT_XS:
            out.append((x, y, 0.7, 0.6, 0.5))  # cushion
            out.append((x, y - 0.38, 0.7, 0.15, 1.0))  # seat back
    return out


def walkways(direction="back"):
    return [
        Zone("left_walkway", -2.4, -1.6, -4.3, 3.5, leave_toward=direction),
        Zone("right_walkway", 1.6, 2.4, -4.3, 3.5, leave_toward=direction),
    ]


def make(mount=OVERHEAD, noise=0.0, dropout=0.0, direction="back", learn=True):
    src = SimDepthSource(mount, width=480, height=300)
    src.furniture = seats()
    src.noise, src.dropout = noise, dropout
    pipe = Pipeline(src.intrinsics, mount, ZoneTracker(walkways(direction)), stride=2)
    if learn:
        pipe.learn_empty_room([src.render() for _ in range(3)])
    return src, pipe


def run(src, pipe, script, t0=0.0, dt=1 / 15):
    """script: list of people lists, one per frame. Returns per-frame results."""
    out = []
    for i, people in enumerate(script):
        src.people = people
        out.append(pipe.process(src.render(), t0 + i * dt))
    return out


SEATED = [(x, y, 1.25) for x in SEAT_XS for y in ROW_YS[:1]]  # front row full


# --- finding people -------------------------------------------------------

@pytest.mark.parametrize("mount", [OVERHEAD, REAR])
def test_counts_seated_people_with_empty_room_map(mount):
    src, pipe = make(mount)
    src.people = SEATED
    found = pipe.finder.find(pipe.points(src.render())[0])
    assert len(found) == len(SEATED)
    assert not any(p.standing for p in found)


def test_empty_room_has_nobody_even_with_seat_backs():
    src, pipe = make()
    assert pipe.finder.find(pipe.points(src.render())[0]) == []


def test_without_map_only_standing_people_are_found():
    src, pipe = make(learn=False)
    src.people = SEATED + [(-2.0, 0.0, 1.75)]
    found = pipe.finder.find(pipe.points(src.render())[0])
    assert len(found) == 1 and found[0].standing
    assert found[0].x == pytest.approx(-2.0, abs=0.15)


@pytest.mark.parametrize("mount, spots", [
    # Overhead at 2.7 m only sees heads within ~1.2 m along the room and ~1.8 m across.
    (OVERHEAD, [(-1.6, 0.8), (0.0, -1.0), (1.5, -0.5)]),
    # A back-wall camera sees the whole room.
    (REAR, [(-2.0, 2.0), (0.0, -2.5), (2.0, -3.5), (-2.0, 3.3)]),
])
def test_person_position_is_accurate(mount, spots):
    src, pipe = make(mount)
    for x, y in spots:
        src.people = [(x, y, 1.75)]
        (p,) = pipe.finder.find(pipe.points(src.render())[0])
        assert (p.x, p.y) == pytest.approx((x, y), abs=0.15)


def test_empty_room_map_is_dropped_when_camera_moves(tmp_path):
    src, pipe = make()
    path = tmp_path / "room.npz"
    pipe.finder.empty_room.save(path, (0.0, 0.0, 2.7, 0.0, 0.0, 0.0))
    assert EmptyRoom.load(path, (0.0, 0.0, 2.7, 0.0, 0.0, 0.0)) is not None
    assert EmptyRoom.load(path, (0.0, 0.0, 2.7, 35.0, 0.0, 0.0)) is None


# --- tracks ----------------------------------------------------------------

def test_track_estimates_walking_speed_and_keeps_its_id():
    tr = PeopleTracker()
    ids = set()
    for i in range(20):
        t = i / 15
        tracks = tr.update([Person(-2.0, 2.0 - 1.0 * t, 1.75, 0.2, True)], t)
        ids |= {k.id for k in tracks}
    (track,) = tracks
    assert ids == {track.id}
    assert track.vy == pytest.approx(-1.0, abs=0.15)
    assert abs(track.vx) < 0.1


def test_track_bridges_a_short_occlusion():
    tr = PeopleTracker(coast_seconds=1.0)
    for i in range(5):
        tr.update([Person(-2.0, 1.0 - 0.07 * i, 1.75, 0.2, True)], i / 15)
    assert len(tr.update([], 0.6)) == 1  # hidden for ~0.33 s: still tracked
    assert tr.update([], 2.0) == []  # gone for over a second: dropped


def test_two_people_passing_keep_separate_tracks():
    tr = PeopleTracker()
    for i in range(15):
        t = i / 15
        tracks = tr.update([Person(-2.0, 1.0 - t, 1.75, 0.2, True), Person(2.0, -1.0 + t, 1.75, 0.2, True)], t)
    by_x = sorted(tracks, key=lambda k: k.x)
    assert by_x[0].vy < -0.7 and by_x[1].vy > 0.7


# --- pre-arming --------------------------------------------------------------

def frames_until_on(results, zone):
    for i, r in enumerate(results):
        if r.zone_changes.get(zone):
            return i
    return None


def walk_out_script(x_seat=-1.2, y=0.6):
    """Seated, stands up, steps into the left walkway, walks toward the back."""
    script = [[(x_seat, y, 1.25)]] * 4 + [[(x_seat, y, 1.75)]] * 6
    for i in range(12):
        script.append([(-2.0, y - 0.07 * i, 1.75)])
    return script


def test_standing_up_prearms_the_nearest_walkway():
    src, pipe = make()
    results = run(src, pipe, walk_out_script())
    armed_frames = [r.armed for r in results[4:10]]
    assert all("left_walkway" in a for a in armed_frames[2:])
    assert not any("right_walkway" in a for a in armed_frames)


def test_prearmed_walkway_reacts_on_the_first_frame():
    src, pipe = make()
    results = run(src, pipe, walk_out_script())
    assert frames_until_on(results, "left_walkway") == 10  # the very frame they step in


def test_without_prearm_it_needs_confirmation_frames():
    src, pipe = make()
    script = [[]] * 4 + [[(-2.0, 0.6 - 0.07 * i, 1.75)] for i in range(12)]  # appears in the walkway
    results = run(src, pipe, script)
    assert frames_until_on(results, "left_walkway") > 4 + 1


def test_walking_back_in_does_not_prearm_or_trigger():
    src, pipe = make()
    script = [[(-2.0, -3.0 + 0.07 * i, 1.75)] for i in range(25)]  # walks from the back toward the seats
    results = run(src, pipe, script)
    assert frames_until_on(results, "left_walkway") is None
    assert not any(r.armed for r in results)


# --- accuracy under realistic noise -----------------------------------------

@pytest.mark.parametrize("mount", [OVERHEAD, REAR])
def test_full_audience_never_triggers_under_noise(mount):
    src, pipe = make(mount, noise=0.01, dropout=0.1, direction="any")
    audience = [(x, y, 1.25) for x in SEAT_XS for y in ROW_YS]
    results = run(src, pipe, [audience] * 30)
    assert not any(r.zone_changes for r in results)
    assert all(len(r.tracks) == len(audience) for r in results[5:])


@pytest.mark.parametrize("mount", [OVERHEAD, REAR])
def test_leaving_lights_only_their_side_under_noise(mount):
    src, pipe = make(mount, noise=0.01, dropout=0.1)
    others = [(x, ROW_YS[1], 1.25) for x in SEAT_XS]
    script = [ppl + others for ppl in walk_out_script()]
    results = run(src, pipe, script)
    changes = {k: v for r in results for k, v in r.zone_changes.items()}
    assert changes == {"left_walkway": True}
    assert frames_until_on(results, "left_walkway") <= 11


# --- room presence ------------------------------------------------------------

def test_room_presence_turns_off_only_after_empty_period():
    rp = RoomPresence(empty_seconds=300, confirm_frames=3)
    assert [rp.update(2, t) for t in (0, 1, 2)] == [None, None, True]
    assert rp.update(0, 3) is None
    assert rp.update(0, 200) is None
    assert rp.update(1, 250) is None  # someone came back
    assert rp.update(0, 260) is None
    assert rp.update(0, 551) is False
