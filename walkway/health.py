"""Keeping it running and knowing it works: camera reconnects, health numbers,
bump detection, event history and the walk test."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


# --- camera reconnects -------------------------------------------------------

class ResilientSource:
    """Wraps a camera so a dropped connection is retried instead of crashing.

    `factory()` opens the camera and returns a source with `.intrinsics` and
    `.frames()`. If no frame arrives for `stall_seconds`, or the camera raises,
    it is closed and reopened with a growing delay (1 s up to 30 s).
    `on_status(online: bool)` is called whenever the camera comes or goes.
    """

    def __init__(self, factory: Callable[[], object], on_status: Callable[[bool], None] | None = None,
                 stall_seconds: float = 5.0, max_backoff: float = 30.0, sleep=time.sleep):
        self.factory = factory
        self.on_status = on_status
        self.stall_seconds = stall_seconds
        self.max_backoff = max_backoff
        self._sleep = sleep
        self.online = False
        self.reconnects = 0
        self._src = self._open_blocking()
        self.intrinsics = self._src.intrinsics

    def _set_online(self, online: bool) -> None:
        if online != self.online:
            self.online = online
            log.info("camera %s", "online" if online else "OFFLINE")
            if self.on_status:
                self.on_status(online)

    def _open_blocking(self):
        delay = 1.0
        while True:
            try:
                src = self.factory()
                self._set_online(True)
                return src
            except Exception as e:  # camera unplugged, PoE down, SDK error
                self._set_online(False)
                log.warning("camera not available (%s); retrying in %.0f s", e, delay)
                self._sleep(delay)
                delay = min(delay * 2, self.max_backoff)

    def frames(self) -> Iterator[np.ndarray]:
        while True:
            try:
                it = iter(frames_with_timeout(self._src, self.stall_seconds))
                for frame in it:
                    yield frame
                raise RuntimeError("camera stream ended")
            except Exception as e:
                self._set_online(False)
                log.warning("camera dropped (%s); reconnecting", e)
                close = getattr(self._src, "close", None)
                if close:
                    try:
                        close()
                    except Exception:
                        pass
                self.reconnects += 1
                self._src = self._open_blocking()
                self.intrinsics = self._src.intrinsics


def frames_with_timeout(src, timeout: float) -> Iterator[np.ndarray]:
    """Use the source's own timeout-aware reader when it has one."""
    reader = getattr(src, "frames_with_timeout", None)
    return reader(timeout) if reader else src.frames()


# --- health numbers ----------------------------------------------------------

@dataclass
class Health:
    """Frame rate and processing delay, smoothed."""

    fps: float = 0.0
    delay_ms: float = 0.0
    frames: int = 0
    _last: float | None = None

    def tick(self, frame_time: float, done_time: float) -> None:
        if self._last is not None:
            dt = frame_time - self._last
            if dt > 0:
                inst = 1.0 / dt
                self.fps = inst if self.frames < 2 else 0.9 * self.fps + 0.1 * inst
        self._last = frame_time
        d = (done_time - frame_time) * 1000
        self.delay_ms = d if self.frames < 2 else 0.9 * self.delay_ms + 0.1 * d
        self.frames += 1


# --- bump detection ----------------------------------------------------------

@dataclass
class BumpCheck:
    """Periodically re-fits the floor and flags a camera that has moved.

    Flags after `confirm` checks in a row disagree with the saved mount by
    more than `height_tol` metres or `angle_tol` degrees, so one bad frame
    (someone lying on the floor, a blanket) doesn't raise an alarm.
    """

    every_seconds: float = 60.0
    height_tol: float = 0.06
    angle_tol: float = 2.5
    confirm: int = 3
    moved: bool = False
    detail: str = ""
    _bad: int = 0
    _last: float = float("-inf")

    def due(self, now: float) -> bool:
        return now - self._last >= self.every_seconds

    def check(self, fit, mount, now: float) -> bool | None:
        """Feed a FloorFit. Returns True/False when `moved` changes, else None."""
        self._last = now
        dh = abs(fit.height - mount.height)
        dt = abs(fit.tilt_deg - mount.tilt_deg)
        dr = abs(fit.roll_deg - mount.roll_deg) if fit.tilt_deg > 3 else 0.0
        off = dh > self.height_tol or dt > self.angle_tol or dr > self.angle_tol
        self._bad = self._bad + 1 if off else 0
        was = self.moved
        if self._bad >= self.confirm:
            self.moved = True
            self.detail = (f"camera looks moved: height {fit.height:.2f} m (saved {mount.height:.2f}), "
                           f"tilt {fit.tilt_deg:.1f}° (saved {mount.tilt_deg:.1f}). Run Auto-level again.")
        elif not off:
            self.moved = False
            self.detail = ""
        return self.moved if self.moved != was else None


# --- event history -----------------------------------------------------------

class EventLog:
    """What happened, when. No images: just event names and numbers.

    Keeps the last `keep` events in memory for the setup page and appends
    every event to a JSON-lines file (rotated at ~1 MB) for later.
    """

    def __init__(self, path: str | Path | None = None, keep: int = 300, max_bytes: int = 1_000_000):
        self.path = Path(path) if path else None
        self.max_bytes = max_bytes
        self.recent: deque = deque(maxlen=keep)
        self._lock = threading.Lock()

    def add(self, kind: str, **data) -> dict:
        ev = {"t": round(time.time(), 2), "kind": kind, **data}
        with self._lock:
            self.recent.append(ev)
            if self.path:
                try:
                    if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                        self.path.replace(self.path.with_suffix(self.path.suffix + ".1"))
                    with self.path.open("a") as f:
                        f.write(json.dumps(ev) + "\n")
                except OSError as e:
                    log.warning("can't write event log: %s", e)
        return ev

    def last(self, n: int = 100) -> list[dict]:
        with self._lock:
            return list(self.recent)[-n:]


# --- walk test ---------------------------------------------------------------

@dataclass
class ZoneTest:
    walk_scores: list[float] = field(default_factory=list)  # someone standing in the zone
    idle_max: float = 0.0  # highest score with nobody standing in it
    triggers: int = 0


@dataclass
class WalkTest:
    """Records how each zone responds while you walk through and sit around,
    then suggests settings.

    Ground truth comes from the tracks: frames with a standing person inside
    a zone count as "walking"; all others show the zone's noise floor
    (seated people next to it, blankets, depth noise).
    """

    running: bool = False
    started: float = 0.0
    zones: dict[str, ZoneTest] = field(default_factory=dict)
    max_people: int = 0
    min_people: int | None = None

    def start(self, zone_names: list[str], now: float) -> None:
        self.running, self.started = True, now
        self.zones = {n: ZoneTest() for n in zone_names}
        self.max_people, self.min_people = 0, None

    def feed(self, zones, scores: dict[str, float], tracks, changes: dict[str, bool], now: float | None = None) -> None:
        if not self.running:
            return
        # Only people actually seen this frame: a track coasting through a gap
        # has no points behind it and would drag the "walking" scores down.
        seen = [k for k in tracks if now is None or k.last_seen >= now - 1e-6]
        for z in zones:
            t = self.zones.setdefault(z.name, ZoneTest())
            s = float(scores.get(z.name, 0))
            walking = any(k.standing and z.x_min <= k.x <= z.x_max and z.y_min <= k.y <= z.y_max for k in seen)
            if walking:
                t.walk_scores.append(s)
            else:
                t.idle_max = max(t.idle_max, s)
            if changes.get(z.name):
                t.triggers += 1
        n = len(tracks)
        self.max_people = max(self.max_people, n)
        self.min_people = n if self.min_people is None else min(self.min_people, n)

    def report(self, zones, now: float) -> dict:
        """Per zone: what was seen and a suggested Points needed."""
        out = {"seconds": round(now - self.started), "zones": [], "people_seen": [self.min_people or 0, self.max_people]}
        for z in zones:
            t = self.zones.get(z.name, ZoneTest())
            row = {"zone": z.name, "current": z.min_points, "triggers": t.triggers,
                   "walk_frames": len(t.walk_scores), "idle_max": round(t.idle_max)}
            if not t.walk_scores:
                row["advice"] = "Nobody walked through this zone during the test. Walk it a few times and test again."
            else:
                weak = float(np.percentile(t.walk_scores, 10))  # a weak but real pass
                row["walk_low"] = round(weak)
                if weak > t.idle_max * 1.5 + 10:
                    # Leave room on both sides: 40% of the way from noise to a weak pass.
                    suggestion = int(round(t.idle_max + 0.4 * (weak - t.idle_max)))
                    row["suggested"] = max(20, suggestion)
                    row["advice"] = ("Clear separation between walking and not walking. "
                                     f"Set Points needed to {row['suggested']}.")
                else:
                    row["advice"] = ("Walking and sitting look too similar here. Raise From height a little, "
                                     "shrink the zone away from the seats, or move the camera.")
            out["zones"].append(row)
        return out

    def stop(self) -> None:
        self.running = False
