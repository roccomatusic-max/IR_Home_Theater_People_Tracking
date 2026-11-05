import json

import numpy as np
import pytest

from walkway.geometry import Intrinsics, Mount, depth_to_room_points
from walkway.mqtt import discovery_messages
from walkway.sources import SimDepthSource
from walkway.zones import Zone, ZoneTracker

MOUNT = Mount(height=2.6)
LEFT = Zone("left_walkway", -2.4, -1.6, -2.0, 2.0)
RIGHT = Zone("right_walkway", 1.6, 2.4, -2.0, 2.0)


def points_with(*people, mount=MOUNT):
    src = SimDepthSource(mount)
    src.people = list(people)
    return depth_to_room_points(src.render(), src.intrinsics, mount)


def test_center_pixel_lands_under_camera():
    intr = Intrinsics(fx=500, fy=500, cx=2, cy=2)
    depth = np.full((5, 5), 2600, dtype=np.uint16)
    pts = depth_to_room_points(depth, intr, Mount(x=1.0, y=-0.5, height=2.6), stride=1)
    center = pts[12]
    assert center == pytest.approx([1.0, -0.5, 0.0], abs=1e-6)


def test_image_right_is_room_right_and_image_top_is_screen():
    intr = Intrinsics(fx=500, fy=500, cx=2, cy=2)
    depth = np.full((5, 5), 2600, dtype=np.uint16)
    pts = depth_to_room_points(depth, intr, Mount(height=2.6), stride=1).reshape(5, 5, 3)
    assert pts[2, 4, 0] > 0  # right column -> +x
    assert pts[0, 2, 1] > 0  # top row -> +y (toward screen)


def test_tilt_moves_view_toward_screen():
    intr = Intrinsics(fx=500, fy=500, cx=0, cy=0)
    depth = np.array([[2000]], dtype=np.uint16)
    p = depth_to_room_points(depth, intr, Mount(height=2.6, tilt_deg=30), stride=1)[0]
    assert p[1] == pytest.approx(2.0 * np.sin(np.radians(30)), abs=1e-4)
    assert p[2] == pytest.approx(2.6 - 2.0 * np.cos(np.radians(30)), abs=1e-4)


def test_empty_room_triggers_nothing():
    pts = points_with()
    assert LEFT.count(pts) == 0 and RIGHT.count(pts) == 0


def test_standing_person_in_left_walkway():
    pts = points_with((-2.0, 0.5, 1.75))
    assert LEFT.count(pts) >= LEFT.min_points
    assert RIGHT.count(pts) == 0


def test_seated_person_does_not_trigger():
    # Seated head height ~1.2 m sits in the band only as a thin top surface;
    # in the seats (between the walkways) it is outside both zones anyway.
    pts = points_with((-0.8, 0.0, 1.2), (0.8, 0.0, 1.2))
    assert LEFT.count(pts) == 0 and RIGHT.count(pts) == 0


def test_tracker_debounces_on_and_off():
    tracker = ZoneTracker([LEFT, RIGHT], on_frames=3, off_seconds=30)
    walking = points_with((-2.0, 0.0, 1.75))
    empty = points_with()
    assert tracker.update(walking, 0.0) == {}
    assert tracker.update(walking, 0.1) == {}
    assert tracker.update(walking, 0.2) == {"left_walkway": True}
    assert tracker.update(walking, 0.3) == {}
    assert tracker.update(empty, 10.0) == {}  # still within hold time
    assert tracker.update(empty, 30.3) == {"left_walkway": False}


def test_one_noisy_frame_does_not_trigger():
    tracker = ZoneTracker([LEFT], on_frames=3)
    walking = points_with((-2.0, 0.0, 1.75))
    empty = points_with()
    for t, pts in enumerate([walking, walking, empty, walking, empty]):
        assert tracker.update(pts, float(t)) == {}


def test_discovery_payload():
    (topic, payload), = discovery_messages(["left_walkway"])
    cfg = json.loads(payload)
    assert topic == "homeassistant/binary_sensor/theater_walkway_left_walkway/config"
    assert cfg["device_class"] == "occupancy"
    assert cfg["state_topic"] == "theater_walkway/left_walkway/state"


def test_set_zones_keeps_state_for_kept_names():
    tracker = ZoneTracker([LEFT, RIGHT], on_frames=1)
    tracker.update(points_with((-2.0, 0.0, 1.75)), 0.0)
    assert tracker.states["left_walkway"].occupied
    tracker.set_zones([LEFT, Zone("aisle", -0.5, 0.5, -2, 2)])
    assert tracker.states["left_walkway"].occupied
    assert set(tracker.states) == {"left_walkway", "aisle"}


def test_setup_server_draw_and_save(tmp_path):
    import urllib.request

    import yaml

    from walkway.setup_ui import SetupServer

    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"camera": {"height": 2.6}, "zones": [], "mqtt": {"host": "x"}}))
    tracker = ZoneTracker([])
    seen = []
    srv = SetupServer(tracker, cfg, port=0, on_zones_changed=lambda old, new: seen.append((old, new)))
    port = srv.httpd.server_address[1]
    try:
        srv.update(points_with((-2.0, 0.0, 1.75)), with_image=True)
        base = f"http://127.0.0.1:{port}"
        assert urllib.request.urlopen(base + "/").read().startswith(b"<!doctype html>")
        assert urllib.request.urlopen(base + "/map.png").read()[:4] == b"\x89PNG"
        body = json.dumps({"save": True, "zones": [
            {"name": "left walkway", "x_min": -1.6, "x_max": -2.4, "y_min": 2, "y_max": -2},
        ]}).encode()
        req = urllib.request.Request(base + "/zones", data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req).read()
        srv.update(points_with((-2.0, 0.0, 1.75)), with_image=False)
        state = json.loads(urllib.request.urlopen(base + "/state.json").read())
    finally:
        srv.close()
    assert [z.name for z in tracker.zones] == ["left_walkway"]
    assert tracker.zones[0].x_min == -2.4 and tracker.zones[0].y_min == -2
    assert state["counts"]["left_walkway"] >= 150
    assert seen == [([], ["left_walkway"])]
    saved = yaml.safe_load(cfg.read_text())
    assert saved["zones"][0]["name"] == "left_walkway"
    assert saved["mqtt"] == {"host": "x"}  # other settings kept
