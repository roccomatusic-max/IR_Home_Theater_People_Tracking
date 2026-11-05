"""One frame in, decisions out: the whole tracking pipeline, without any I/O.

depth frame
  -> room-frame points (+ distance weights)
  -> people (blobs taller than the empty room) -> tracks (smoothed, with speed)
  -> pre-arm walkways next to anyone standing in the seats
  -> walkway zones on/off, room occupied / people count
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import numpy as np

from .geometry import Intrinsics, Mount, depth_to_room_points
from .people import EmptyRoom, PeopleFinder, Person
from .tracks import PeopleTracker, Track
from .zones import RoomPresence, Zone, ZoneTracker


@dataclass
class FrameResult:
    points: np.ndarray
    weights: np.ndarray
    people: list[Person]
    tracks: list[Track]
    armed: list[str]
    zone_changes: dict[str, bool]
    room_change: bool | None


@dataclass
class Pipeline:
    intrinsics: Intrinsics
    mount: Mount
    zones: ZoneTracker
    finder: PeopleFinder = field(default_factory=PeopleFinder)
    people: PeopleTracker = field(default_factory=PeopleTracker)
    presence: RoomPresence = field(default_factory=RoomPresence)
    stride: int = 4
    lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def room_mapped(self) -> bool:
        """Seated people (and so room occupancy) need a learned empty-room map."""
        return self.finder.empty_room is not None

    def points(self, depth: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return depth_to_room_points(depth, self.intrinsics, self.mount, stride=self.stride, with_weights=True)

    def process(self, depth: np.ndarray, now: float) -> FrameResult:
        points, weights = self.points(depth)
        people = self.finder.find(points)
        tracks = self.people.update(people, now)
        with self.lock:
            armed = self.zones.prearm(tracks, now)
            changes = self.zones.update(points, now, weights, tracks)
        room_change = self.presence.update(len(tracks), now) if self.room_mapped else None
        return FrameResult(points, weights, people, tracks, armed, changes, room_change)

    def learn_empty_room(self, depth_frames: list[np.ndarray]) -> EmptyRoom:
        room = EmptyRoom.learn([self.points(d)[0] for d in depth_frames])
        self.finder.empty_room = room
        self.people.tracks.clear()
        return room

    def set_mount(self, mount: Mount) -> None:
        """New camera angle: the old empty-room map no longer lines up."""
        self.mount = mount
        self.finder.empty_room = None
        self.people.tracks.clear()


def make_zone_tracker(zones: list[Zone], cfg) -> ZoneTracker:
    return ZoneTracker(
        zones,
        on_frames=cfg.on_frames,
        off_seconds=cfg.off_seconds,
        return_off_seconds=cfg.return_off_seconds,
        arm_distance=float(cfg.room.get("arm_distance", 1.2)),
        arm_seconds=float(cfg.room.get("arm_seconds", 8.0)),
    )
