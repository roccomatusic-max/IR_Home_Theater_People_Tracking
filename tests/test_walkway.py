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


# --- rear-wall mounting -------------------------------------------------

REAR = Mount(y=-4.5, height=2.7, tilt_deg=50)
LONG_LEFT = Zone("left_walkway", -2.4, -1.6, -4.4, 4.0)


def weighted(*people, mount=REAR):
    src = SimDepthSource(mount)
    src.people = list(people)
    return depth_to_room_points(src.render(), src.intrinsics, mount, with_weights=True)


@pytest.mark.parametrize("y", [-4.0, -2.0, 0.0, 2.0, 3.5])
def test_rear_camera_sees_walker_anywhere_along_the_room(y):
    pts, w = weighted((-2.0, y, 1.75))
    assert LONG_LEFT.count(pts, w) >= LONG_LEFT.min_points


def test_rear_camera_distance_weighting_evens_out_near_and_far():
    near = LONG_LEFT.count(*weighted((-2.0, -4.0, 1.75)))
    far = LONG_LEFT.count(*weighted((-2.0, 3.5, 1.75)))
    raw_near = LONG_LEFT.count(weighted((-2.0, -4.0, 1.75))[0])
    raw_far = LONG_LEFT.count(weighted((-2.0, 3.5, 1.75))[0])
    assert raw_near > 20 * raw_far  # unweighted, far people almost vanish
    assert 0.5 < near / far < 2.0  # weighted, they score about the same


def test_rear_camera_ignores_seated_people():
    pts, w = weighted((-1.0, 0.0, 1.15), (1.0, -1.0, 1.15), (0.0, 1.0, 1.15))
    assert LONG_LEFT.count(pts, w) == 0


@pytest.mark.parametrize(
    "mount",
    [Mount(height=2.6), REAR, Mount(height=2.9, tilt_deg=35, roll_deg=4), Mount(y=-4, height=2.5, tilt_deg=60, roll_deg=-3)],
)
def test_auto_level_recovers_height_tilt_roll(mount):
    from walkway.geometry import depth_to_camera_points, fit_floor

    src = SimDepthSource(mount)
    src.people = [(-2.0, 0.0, 1.75), (1.0, 1.0, 1.2)]
    depth = src.render().astype(np.float32)
    rng = np.random.default_rng(1)
    depth *= 1 + rng.normal(0, 0.005, depth.shape)  # ~0.5% depth noise
    fit = fit_floor(depth_to_camera_points(depth.astype(np.uint16), src.intrinsics, stride=2))
    assert fit.height == pytest.approx(mount.height, abs=0.03)
    assert fit.tilt_deg == pytest.approx(mount.tilt_deg, abs=1.0)
    assert fit.roll_deg == pytest.approx(mount.roll_deg, abs=1.0)


def test_save_camera_keeps_other_settings(tmp_path):
    import yaml

    from walkway.config import load, save_camera

    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"camera": {"x": 0.5}, "zones": [{"name": "a", "x_min": 0, "x_max": 1, "y_min": 0, "y_max": 1}]}))
    save_camera(p, height=2.71234, tilt_deg=48.2, roll_deg=-1.0)
    cfg = load(p)
    assert cfg.mount == Mount(x=0.5, height=2.712, tilt_deg=48.2, roll_deg=-1.0)
    assert cfg.zones[0].name == "a"


# --- only light the way out --------------------------------------------

def walk(tracker, ys, t0=0.0, dt=0.1, zone_x=-2.0):
    """Feed frames of one person walking through the given y positions."""
    events = []
    for i, y in enumerate(ys):
        pts = points_with((zone_x, y, 1.75)) if y is not None else points_with()
        events.append(tracker.update(pts, t0 + i * dt))
    return {k: v for e in events for k, v in e.items()}


def test_leaving_toward_back_turns_on():
    z = Zone("left_walkway", -2.4, -1.6, -2.0, 2.0, leave_toward="back")
    tracker = ZoneTracker([z])
    assert walk(tracker, np.linspace(1.0, -1.0, 15)) == {"left_walkway": True}


def test_returning_to_seat_does_not_turn_on():
    z = Zone("left_walkway", -2.4, -1.6, -2.0, 2.0, leave_toward="back")
    tracker = ZoneTracker([z])
    assert walk(tracker, np.linspace(-1.0, 1.0, 15)) == {}


def test_standing_still_in_walkway_does_not_turn_on_directional_zone():
    z = Zone("left_walkway", -2.4, -1.6, -2.0, 2.0, leave_toward="back")
    tracker = ZoneTracker([z])
    assert walk(tracker, [0.0] * 15) == {}


def test_coming_back_turns_lights_off_sooner():
    z = Zone("left_walkway", -2.4, -1.6, -2.0, 2.0, leave_toward="back")
    tracker = ZoneTracker([z], off_seconds=30, return_off_seconds=5)
    walk(tracker, np.linspace(1.0, -1.5, 15))  # leaves: lights on
    assert tracker.states["left_walkway"].occupied
    walk(tracker, list(np.linspace(-1.5, 1.0, 15)) + [None], t0=60.0)  # comes back, sits down
    assert walk(tracker, [None], t0=64.0) == {}
    assert walk(tracker, [None], t0=67.0) == {"left_walkway": False}


def test_leave_toward_screen():
    z = Zone("right_walkway", 1.6, 2.4, -2.0, 2.0, leave_toward="screen")
    tracker = ZoneTracker([z])
    assert walk(tracker, np.linspace(-1.0, 1.0, 15), zone_x=2.0) == {"right_walkway": True}


def test_bad_leave_direction_rejected():
    with pytest.raises(ValueError):
        Zone("x", 0, 1, 0, 1, leave_toward="sideways")


# --- movie-mode switch --------------------------------------------------

def test_switch_off_clears_and_pauses_detection():
    tracker = ZoneTracker([LEFT], on_frames=1)
    walking = points_with((-2.0, 0.0, 1.75))
    tracker.update(walking, 0.0)
    assert tracker.set_enabled(False) == {"left_walkway": False}
    assert tracker.update(walking, 1.0) == {}
    assert not tracker.states["left_walkway"].occupied
    tracker.set_enabled(True)
    assert tracker.update(walking, 2.0) == {"left_walkway": True}


def test_switch_discovery_payload():
    from walkway.mqtt import SWITCH_SET, SWITCH_STATE, switch_discovery

    topic, payload = switch_discovery()
    cfg = json.loads(payload)
    assert topic == "homeassistant/switch/theater_walkway_detection/config"
    assert cfg["command_topic"] == SWITCH_SET and cfg["state_topic"] == SWITCH_STATE


def make_fake_publisher():
    import threading
    from types import SimpleNamespace

    from walkway.mqtt import SWITCHES, Publisher

    published, seen = [], []
    pub = Publisher.__new__(Publisher)  # skip the network connection
    pub._client = SimpleNamespace(publish=lambda t, p, retain=False: published.append((t, p)))
    pub._switch = {k: d for k, (_, d, _) in SWITCHES.items()}
    pub._restored = {k: threading.Event() for k in SWITCHES}
    pub._lock = threading.Lock()
    pub._room = None
    pub.on_switch = lambda k, v: seen.append((k, v))
    return pub, published, seen


def mqtt_msg(topic, value):
    from types import SimpleNamespace

    return SimpleNamespace(topic=topic, payload=value.encode())


def test_switch_restores_last_state_and_obeys_commands():
    from walkway.mqtt import SWITCH_SET, SWITCH_STATE

    pub, published, seen = make_fake_publisher()
    pub._on_message(None, None, mqtt_msg(SWITCH_STATE, "OFF"))  # retained state from before a restart
    assert seen == [("detection", False)] and published == []
    pub._on_message(None, None, mqtt_msg(SWITCH_STATE, "ON"))  # our own echo later: ignored
    assert seen == [("detection", False)]
    pub._on_message(None, None, mqtt_msg(SWITCH_SET, "ON"))  # user flips it in Home Assistant
    assert seen[-1] == ("detection", True) and published == [(SWITCH_STATE, "ON")]


def test_auto_off_switch_defaults_off_and_is_separate():
    from walkway.mqtt import switch_topics

    pub, published, seen = make_fake_publisher()
    assert pub.switch("empty_auto_off") is False and pub.switch("detection") is True
    pub._on_message(None, None, mqtt_msg(switch_topics("empty_auto_off")[1], "ON"))
    assert pub.switch("empty_auto_off") is True and pub.switch("detection") is True
    assert seen == [("empty_auto_off", True)]


def test_room_sensors_publish_only_on_change():
    from walkway.mqtt import PEOPLE_TOPIC, ROOM_TOPIC, room_discovery

    pub, published, _ = make_fake_publisher()
    pub.room(2, True)
    pub.room(2, True)
    pub.room(0, True)
    assert published == [(PEOPLE_TOPIC, "2"), (ROOM_TOPIC, "ON"), (PEOPLE_TOPIC, "0"), (ROOM_TOPIC, "ON")]
    topics = [t for t, _ in room_discovery()]
    assert topics == ["homeassistant/sensor/theater_walkway_people/config",
                      "homeassistant/binary_sensor/theater_walkway_room/config"]
