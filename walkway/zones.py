"""Walkway zones: 3D boxes on the floor plan with on/off debouncing and direction."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

LEAVE_DIRECTIONS = ("any", "back", "screen")


@dataclass
class Zone:
    name: str
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    # Only points in this height band count. With z_min around 1.0 m a
    # seated person barely registers, while someone standing fills the band.
    z_min: float = 1.0
    z_max: float = 2.1
    # Detection threshold. Points are weighted by distance from the camera,
    # so this means "points as seen from 2 m away" and works the same for a
    # person right under the camera or at the far end of the room.
    min_points: int = 100
    # Which way someone walks when they LEAVE: "back" (toward the back wall),
    # "screen", or "any". With back/screen, walking the other way (returning
    # to a seat) doesn't turn the zone on.
    leave_toward: str = "any"

    def __post_init__(self) -> None:
        if self.leave_toward not in LEAVE_DIRECTIONS:
            raise ValueError(f"{self.name}: leave_toward must be one of {LEAVE_DIRECTIONS}")

    def mask(self, points: np.ndarray) -> np.ndarray:
        x, y, z = points[:, 0], points[:, 1], points[:, 2]
        return (
            (x >= self.x_min) & (x <= self.x_max)
            & (y >= self.y_min) & (y <= self.y_max)
            & (z >= self.z_min) & (z <= self.z_max)
        )

    def count(self, points: np.ndarray, weights: np.ndarray | None = None) -> int:
        m = self.mask(points)
        if weights is None:
            return int(np.count_nonzero(m))
        return int(round(float(weights[m].sum())))


@dataclass
class ZoneState:
    occupied: bool = False
    hits_in_a_row: int = 0
    last_seen: float = float("-inf")
    returning: bool = False  # someone is walking back toward the seats
    track: deque = field(default_factory=lambda: deque(maxlen=60))  # (time, centre y) while seen


@dataclass
class ZoneTracker:
    """Debounces raw per-frame detections into stable on/off states.

    A zone turns ON after `on_frames` consecutive frames with a detection
    (and, for zones with a leave direction, once the person has moved at
    least `min_travel` metres that way). It turns OFF once nothing has been
    seen for `off_seconds`, or `return_off_seconds` if the last person was
    walking back toward the seats.
    """

    zones: list[Zone]
    on_frames: int = 3
    off_seconds: float = 30.0
    return_off_seconds: float = 5.0
    min_travel: float = 0.15
    track_seconds: float = 2.0
    enabled: bool = True
    states: dict[str, ZoneState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for z in self.zones:
            self.states.setdefault(z.name, ZoneState())

    def set_zones(self, zones: list[Zone]) -> None:
        """Swap in new zones (from the setup page), keeping state for names that stay."""
        self.zones = list(zones)
        self.states = {z.name: self.states.get(z.name, ZoneState()) for z in self.zones}

    def set_enabled(self, enabled: bool) -> dict[str, bool]:
        """Movie-mode switch. Turning off clears every zone; returns zones that changed."""
        self.enabled = enabled
        changed = {}
        if not enabled:
            for name, st in self.states.items():
                if st.occupied:
                    changed[name] = False
                self.states[name] = ZoneState()
        return changed

    def _travel(self, zone: Zone, st: ZoneState, now: float) -> float:
        """Metres moved in the leave direction over the recent track (negative = returning)."""
        while st.track and now - st.track[0][0] > self.track_seconds:
            st.track.popleft()
        if len(st.track) < 2:
            return 0.0
        dy = st.track[-1][1] - st.track[0][1]
        return -dy if zone.leave_toward == "back" else dy

    def update(self, points: np.ndarray, now: float, weights: np.ndarray | None = None) -> dict[str, bool]:
        """Feed one frame. Returns {zone_name: occupied} for zones that changed."""
        if not self.enabled:
            return {}
        changed: dict[str, bool] = {}
        for zone in self.zones:
            st = self.states[zone.name]
            m = zone.mask(points)
            score = int(np.count_nonzero(m)) if weights is None else float(weights[m].sum())
            if score >= zone.min_points:
                st.hits_in_a_row += 1
                st.last_seen = now
                if zone.leave_toward != "any":
                    st.track.append((now, float(np.median(points[m, 1]))))
                travel = self._travel(zone, st, now) if zone.leave_toward != "any" else 0.0
                heading_out = zone.leave_toward == "any" or travel >= self.min_travel
                if st.occupied and travel <= -2 * self.min_travel:
                    st.returning = True
                elif travel >= self.min_travel:
                    st.returning = False
                if not st.occupied and st.hits_in_a_row >= self.on_frames and heading_out:
                    st.occupied = True
                    st.returning = False
                    changed[zone.name] = True
            else:
                st.hits_in_a_row = 0
                st.track.clear()
                hold = self.return_off_seconds if st.returning else self.off_seconds
                if st.occupied and now - st.last_seen >= hold:
                    st.occupied = False
                    st.returning = False
                    changed[zone.name] = False
        return changed

    def counts(self, points: np.ndarray, weights: np.ndarray | None = None) -> dict[str, int]:
        return {z.name: z.count(points, weights) for z in self.zones}
