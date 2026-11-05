"""Home Assistant integration over MQTT.

Each zone appears as an occupancy binary sensor, and a "Walkway detection"
switch (movie mode) turns detection on and off. Everything is set up through
MQTT discovery, so nothing has to be added by hand in Home Assistant.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)

BASE = "theater_walkway"
DEVICE = {"identifiers": [BASE], "name": "Theater walkway tracker", "model": "OAK-D Pro W depth"}

# Switches shown in Home Assistant: key -> (name, default, icon)
SWITCHES = {
    "detection": ("Walkway detection", True, "mdi:motion-sensor"),
    "empty_auto_off": ("Auto-off when room is empty", False, "mdi:power-sleep"),
}
SWITCH_STATE = f"{BASE}/detection/state"  # kept for the movie-mode switch
SWITCH_SET = f"{BASE}/detection/set"
PEOPLE_TOPIC = f"{BASE}/room/people"
ROOM_TOPIC = f"{BASE}/room/occupied"


def switch_topics(key: str) -> tuple[str, str]:
    return f"{BASE}/{key}/state", f"{BASE}/{key}/set"


def discovery_messages(zone_names: list[str], discovery_prefix: str = "homeassistant") -> list[tuple[str, str]]:
    """(topic, payload) pairs that make each zone appear in Home Assistant."""
    msgs = []
    for name in zone_names:
        cfg = {
            "name": name.replace("_", " ").title(),
            "unique_id": f"{BASE}_{name}",
            "object_id": f"{BASE}_{name}",
            "state_topic": f"{BASE}/{name}/state",
            "availability_topic": f"{BASE}/status",
            "device_class": "occupancy",
            "payload_on": "ON",
            "payload_off": "OFF",
            "device": DEVICE,
        }
        msgs.append((f"{discovery_prefix}/binary_sensor/{BASE}_{name}/config", json.dumps(cfg)))
    return msgs


def switch_discovery(discovery_prefix: str = "homeassistant", key: str = "detection") -> tuple[str, str]:
    name, _, icon = SWITCHES[key]
    state, command = switch_topics(key)
    cfg = {
        "name": name,
        "unique_id": f"{BASE}_{key}",
        "object_id": f"{BASE}_{key}",
        "command_topic": command,
        "state_topic": state,
        "availability_topic": f"{BASE}/status",
        "payload_on": "ON",
        "payload_off": "OFF",
        "icon": icon,
        "device": DEVICE,
    }
    return f"{discovery_prefix}/switch/{BASE}_{key}/config", json.dumps(cfg)


def room_discovery(discovery_prefix: str = "homeassistant") -> list[tuple[str, str]]:
    """People-count sensor and room-occupied binary sensor."""
    common = {"availability_topic": f"{BASE}/status", "device": DEVICE}
    people = {
        "name": "People in theater", "unique_id": f"{BASE}_people", "object_id": f"{BASE}_people",
        "state_topic": PEOPLE_TOPIC, "state_class": "measurement", "icon": "mdi:account-group", **common,
    }
    room = {
        "name": "Theater occupied", "unique_id": f"{BASE}_room", "object_id": f"{BASE}_room",
        "state_topic": ROOM_TOPIC, "device_class": "occupancy", "payload_on": "ON", "payload_off": "OFF", **common,
    }
    return [
        (f"{discovery_prefix}/sensor/{BASE}_people/config", json.dumps(people)),
        (f"{discovery_prefix}/binary_sensor/{BASE}_room/config", json.dumps(room)),
    ]


HEALTH_TOPIC = f"{BASE}/health"


def health_discovery(discovery_prefix: str = "homeassistant") -> list[tuple[str, str]]:
    """Camera online, frame rate, delay and "camera moved" sensors, all read from one JSON topic."""
    common = {"state_topic": HEALTH_TOPIC, "availability_topic": f"{BASE}/status", "device": DEVICE,
              "entity_category": "diagnostic"}
    items = [
        ("binary_sensor", "camera", {"name": "Camera", "device_class": "connectivity",
                                     "value_template": "{{ 'ON' if value_json.online else 'OFF' }}",
                                     "payload_on": "ON", "payload_off": "OFF"}),
        ("binary_sensor", "camera_moved", {"name": "Camera moved", "device_class": "problem",
                                           "value_template": "{{ 'ON' if value_json.moved else 'OFF' }}",
                                           "payload_on": "ON", "payload_off": "OFF"}),
        ("sensor", "fps", {"name": "Frame rate", "unit_of_measurement": "fps", "state_class": "measurement",
                           "value_template": "{{ value_json.fps }}"}),
        ("sensor", "delay", {"name": "Processing delay", "unit_of_measurement": "ms", "state_class": "measurement",
                             "device_class": "duration", "value_template": "{{ value_json.delay_ms }}"}),
    ]
    out = []
    for kind, key, cfg in items:
        cfg = {**cfg, **common, "unique_id": f"{BASE}_{key}", "object_id": f"{BASE}_{key}"}
        out.append((f"{discovery_prefix}/{kind}/{BASE}_{key}/config", json.dumps(cfg)))
    return out


class Publisher:
    """MQTT connection to Home Assistant. Reconnects on its own if the broker restarts."""

    def __init__(
        self,
        host: str,
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        discovery_prefix: str = "homeassistant",
        on_switch: Callable[[str, bool], None] | None = None,
    ):
        import paho.mqtt.client as mqtt

        self.prefix = discovery_prefix
        self.on_switch = on_switch  # called with (key, value)
        self._zones: list[str] = []
        self._states: dict[str, bool] = {}
        self._switch = {k: default for k, (_, default, _) in SWITCHES.items()}
        self._restored = {k: threading.Event() for k in SWITCHES}
        self._room: tuple[int, bool] | None = None
        self._health: dict | None = None
        self._lock = threading.Lock()
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=BASE)
        if username:
            self._client.username_pw_set(username, password)
        self._client.will_set(f"{BASE}/status", "offline", retain=True)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.reconnect_delay_set(1, 30)
        self._client.connect(host, port)
        self._client.loop_start()

    # --- connection -----------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            log.warning("MQTT connect failed: %s", reason_code)
            return
        log.info("connected to MQTT")
        client.subscribe([(t, 0) for k in SWITCHES for t in switch_topics(k)])
        client.publish(f"{BASE}/status", "online", retain=True)
        for key in SWITCHES:
            topic, payload = switch_discovery(self.prefix, key)
            client.publish(topic, payload, retain=True)
        with self._lock:
            zones, states, room = list(self._zones), dict(self._states), self._room
        for t, p in discovery_messages(zones, self.prefix):
            client.publish(t, p, retain=True)
        for name in zones:
            client.publish(f"{BASE}/{name}/state", "ON" if states.get(name) else "OFF", retain=True)
        for t, p in health_discovery(self.prefix):
            client.publish(t, p, retain=True)
        if self._health is not None:
            client.publish(HEALTH_TOPIC, json.dumps(self._health), retain=True)
        if room is not None:
            self.announce_room()
            client.publish(PEOPLE_TOPIC, str(room[0]), retain=True)
            client.publish(ROOM_TOPIC, "ON" if room[1] else "OFF", retain=True)

    def _on_message(self, client, userdata, msg):
        value = msg.payload.decode(errors="ignore").strip().upper()
        if value not in ("ON", "OFF"):
            return
        for key in SWITCHES:
            state_topic, set_topic = switch_topics(key)
            if msg.topic == state_topic:
                # Our own retained state from before a restart: restore it once.
                if not self._restored[key].is_set():
                    self._restored[key].set()
                    self._set_switch(key, value == "ON", publish=False)
                return
            if msg.topic == set_topic:
                self._restored[key].set()
                self._set_switch(key, value == "ON", publish=True)
                return

    def _set_switch(self, key: str, value: bool, publish: bool) -> None:
        changed = value != self._switch[key]
        self._switch[key] = value
        if publish:
            self._client.publish(switch_topics(key)[0], "ON" if value else "OFF", retain=True)
        if changed:
            log.info("%s %s", SWITCHES[key][0], "ON" if value else "OFF")
            if self.on_switch:
                self.on_switch(key, value)

    def switch(self, key: str) -> bool:
        return self._switch[key]

    def wait_for_restore(self, timeout: float = 2.0) -> None:
        """Give the broker a moment to send back the switches' last states, else use defaults."""
        for key, ev in self._restored.items():
            if not ev.wait(timeout):
                ev.set()
                self._client.publish(switch_topics(key)[0], "ON" if self._switch[key] else "OFF", retain=True)
            timeout = 0.1  # the broker sends all retained states together

    # --- health ---------------------------------------------------------
    def health(self, online: bool, fps: float, delay_ms: float, moved: bool) -> None:
        self._health = {"online": online, "fps": round(fps, 1), "delay_ms": round(delay_ms), "moved": moved}
        self._client.publish(HEALTH_TOPIC, json.dumps(self._health), retain=True)

    # --- room -----------------------------------------------------------
    def announce_room(self) -> None:
        for topic, payload in room_discovery(self.prefix):
            self._client.publish(topic, payload, retain=True)

    def room(self, people: int, occupied: bool) -> None:
        with self._lock:
            if self._room == (people, occupied):
                return
            self._room = (people, occupied)
        self._client.publish(PEOPLE_TOPIC, str(people), retain=True)
        self._client.publish(ROOM_TOPIC, "ON" if occupied else "OFF", retain=True)

    # --- zones ----------------------------------------------------------
    def announce(self, zone_names: list[str]) -> None:
        with self._lock:
            for n in zone_names:
                if n not in self._zones:
                    self._zones.append(n)
                    self._states[n] = False
        for topic, payload in discovery_messages(zone_names, self.prefix):
            self._client.publish(topic, payload, retain=True)
        for n in zone_names:
            self._client.publish(f"{BASE}/{n}/state", "OFF", retain=True)

    def forget(self, zone: str) -> None:
        """Remove a deleted zone's sensor from Home Assistant."""
        with self._lock:
            if zone in self._zones:
                self._zones.remove(zone)
            self._states.pop(zone, None)
        self._client.publish(f"{self.prefix}/binary_sensor/{BASE}_{zone}/config", "", retain=True)

    def state(self, zone: str, occupied: bool) -> None:
        log.info("%s -> %s", zone, "ON" if occupied else "OFF")
        with self._lock:
            self._states[zone] = occupied
        self._client.publish(f"{BASE}/{zone}/state", "ON" if occupied else "OFF", retain=True)
