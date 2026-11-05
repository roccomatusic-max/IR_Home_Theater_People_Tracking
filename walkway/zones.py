"""Walkway zones: 3D boxes on the floor plan with on/off debouncing."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class Zone:
    name: str
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    # Only points in this height band count. With z_min around 1.0 m a
    # seated person (head ~1.1-1.2 m but mostly below) barely registers,
    # while someone standing or walking fills the band.
    z_min: float = 1.0
    z_max: float = 2.1
    min_points: int = 150  # points in the box needed for "someone is here"

    def count(self, points: np.ndarray) -> int:
        x, y, z = points[:, 0], points[:, 1], points[:, 2]
        inside = (
            (x >= self.x_min) & (x <= self.x_max)
            & (y >= self.y_min) & (y <= self.y_max)
            & (z >= self.z_min) & (z <= self.z_max)
        )
        return int(np.count_nonzero(inside))


@dataclass
class ZoneState:
    occupied: bool = False
    hits_in_a_row: int = 0
    last_seen: float = float("-inf")


@dataclass
class ZoneTracker:
    """Debounces raw per-frame detections into stable on/off states.

    A zone turns ON after `on_frames` consecutive frames with a detection,
    and OFF once nothing has been seen for `off_seconds`.
    """

    zones: list[Zone]
    on_frames: int = 3
    off_seconds: float = 30.0
    states: dict[str, ZoneState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for z in self.zones:
            self.states.setdefault(z.name, ZoneState())

    def set_zones(self, zones: list[Zone]) -> None:
        """Swap in new zones (from the setup page), keeping state for names that stay."""
        self.zones = list(zones)
        self.states = {z.name: self.states.get(z.name, ZoneState()) for z in self.zones}

    def update(self, points: np.ndarray, now: float) -> dict[str, bool]:
        """Feed one frame. Returns {zone_name: occupied} for zones that changed."""
        changed: dict[str, bool] = {}
        for zone in self.zones:
            st = self.states[zone.name]
            hit = zone.count(points) >= zone.min_points
            if hit:
                st.hits_in_a_row += 1
                st.last_seen = now
                if not st.occupied and st.hits_in_a_row >= self.on_frames:
                    st.occupied = True
                    changed[zone.name] = True
            else:
                st.hits_in_a_row = 0
                if st.occupied and now - st.last_seen >= self.off_seconds:
                    st.occupied = False
                    changed[zone.name] = False
        return changed

    def counts(self, points: np.ndarray) -> dict[str, int]:
        return {z.name: z.count(points) for z in self.zones}
