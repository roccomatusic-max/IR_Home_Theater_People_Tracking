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
SWITCH_STATE = f"{BASE}/detection/state"
SWITCH_SET = f"{BASE}/detection/set"
DEVICE = {"identifiers": [BASE], "name": "Theater walkway tracker", "model": "OAK-D Pro W depth"}


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


def switch_discovery(discovery_prefix: str = "homeassistant") -> tuple[str, str]:
    cfg = {
        "name": "Walkway detection",
        "unique_id": f"{BASE}_detection",
        "object_id": f"{BASE}_detection",
        "command_topic": SWITCH_SET,
        "state_topic": SWITCH_STATE,
        "availability_topic": f"{BASE}/status",
        "payload_on": "ON",
        "payload_off": "OFF",
        "icon": "mdi:motion-sensor",
        "device": DEVICE,
    }
    return f"{discovery_prefix}/switch/{BASE}_detection/config", json.dumps(cfg)


class Publisher:
    """MQTT connection to Home Assistant. Reconnects on its own if the broker restarts."""

    def __init__(
        self,
        host: str,
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        discovery_prefix: str = "homeassistant",
        on_switch: Callable[[bool], None] | None = None,
    ):
        import paho.mqtt.client as mqtt

        self.prefix = discovery_prefix
        self.on_switch = on_switch
        self._zones: list[str] = []
        self._states: dict[str, bool] = {}
        self._enabled = True
        self._restored = threading.Event()
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
        client.subscribe([(SWITCH_SET, 0), (SWITCH_STATE, 0)])
        client.publish(f"{BASE}/status", "online", retain=True)
        topic, payload = switch_discovery(self.prefix)
        client.publish(topic, payload, retain=True)
        with self._lock:
            zones, states = list(self._zones), dict(self._states)
        for t, p in discovery_messages(zones, self.prefix):
            client.publish(t, p, retain=True)
        for name in zones:
            client.publish(f"{BASE}/{name}/state", "ON" if states.get(name) else "OFF", retain=True)

    def _on_message(self, client, userdata, msg):
        value = msg.payload.decode(errors="ignore").strip().upper()
        if value not in ("ON", "OFF"):
            return
        enabled = value == "ON"
        if msg.topic == SWITCH_STATE:
            # Our own retained state from before a restart: restore it once.
            if not self._restored.is_set():
                self._restored.set()
                self._set_enabled(enabled, publish=False)
            return
        self._restored.set()
        self._set_enabled(enabled, publish=True)

    def _set_enabled(self, enabled: bool, publish: bool) -> None:
        changed = enabled != self._enabled
        self._enabled = enabled
        if publish:
            self._client.publish(SWITCH_STATE, "ON" if enabled else "OFF", retain=True)
        if changed:
            log.info("walkway detection %s", "ON" if enabled else "OFF")
            if self.on_switch:
                self.on_switch(enabled)

    def wait_for_restore(self, timeout: float = 2.0) -> None:
        """Give the broker a moment to send back the switch's last state, else default to ON."""
        if not self._restored.wait(timeout):
            self._restored.set()
            self._client.publish(SWITCH_STATE, "ON" if self._enabled else "OFF", retain=True)

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
