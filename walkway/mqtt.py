"""Publish walkway zones to Home Assistant as occupancy binary sensors."""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

BASE = "theater_walkway"


def discovery_messages(zone_names: list[str], discovery_prefix: str = "homeassistant") -> list[tuple[str, str]]:
    """(topic, payload) pairs that make each zone appear in Home Assistant."""
    device = {"identifiers": [BASE], "name": "Theater walkway tracker", "model": "OAK-D Pro W depth"}
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
            "device": device,
        }
        msgs.append((f"{discovery_prefix}/binary_sensor/{BASE}_{name}/config", json.dumps(cfg)))
    return msgs


class Publisher:
    def __init__(self, host: str, port: int = 1883, username: str | None = None, password: str | None = None):
        import paho.mqtt.client as mqtt

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=BASE)
        if username:
            self._client.username_pw_set(username, password)
        self._client.will_set(f"{BASE}/status", "offline", retain=True)
        self._client.connect(host, port)
        self._client.loop_start()
        self._client.publish(f"{BASE}/status", "online", retain=True)

    def announce(self, zone_names: list[str], discovery_prefix: str = "homeassistant") -> None:
        for topic, payload in discovery_messages(zone_names, discovery_prefix):
            self._client.publish(topic, payload, retain=True)

    def state(self, zone: str, occupied: bool) -> None:
        log.info("%s -> %s", zone, "ON" if occupied else "OFF")
        self._client.publish(f"{BASE}/{zone}/state", "ON" if occupied else "OFF", retain=True)
