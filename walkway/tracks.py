"""Follow each person from frame to frame.

Per-frame detections jitter and briefly disappear (someone walks behind a
seat back, or another person blocks the camera). Tracks smooth the position,
estimate walking speed and direction, and bridge short gaps, which makes the
walkway decisions steadier and faster. A track id is a number only; it is
forgotten when the person leaves and is never stored.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .people import Person


@dataclass
class Track:
    id: int
    x: float
    y: float
    vx: float = 0.0
    vy: float = 0.0
    height: float = 0.0
    standing: bool = False
    hits: int = 1
    last_seen: float = 0.0
    last_update: float = 0.0
    stood_up_at: float | None = None  # when this person last went from seated to standing

    def predict(self, now: float) -> tuple[float, float]:
        dt = max(0.0, now - self.last_update)
        return self.x + self.vx * dt, self.y + self.vy * dt

    @property
    def speed(self) -> float:
        return (self.vx ** 2 + self.vy ** 2) ** 0.5


@dataclass
class PeopleTracker:
    """Alpha-beta filter per person, greedy nearest-neighbour matching."""

    match_distance: float = 0.8  # m; a detection further than this from a track is someone else
    coast_seconds: float = 1.0  # keep a track alive this long without a detection
    confirm_hits: int = 2  # detections needed before a track counts
    alpha: float = 0.6  # position smoothing (1 = trust each frame fully)
    beta: float = 0.25  # speed smoothing
    max_speed: float = 3.0  # m/s; faster than a person runs indoors = a bad match
    tracks: list[Track] = field(default_factory=list)
    _next_id: int = 1

    def update(self, people: list[Person], now: float) -> list[Track]:
        pairs = []
        for ti, t in enumerate(self.tracks):
            px, py = t.predict(now)
            for pi, p in enumerate(people):
                d = ((p.x - px) ** 2 + (p.y - py) ** 2) ** 0.5
                if d <= self.match_distance:
                    pairs.append((d, ti, pi))
        pairs.sort()
        used_t, used_p = set(), set()
        for _, ti, pi in pairs:
            if ti in used_t or pi in used_p:
                continue
            used_t.add(ti)
            used_p.add(pi)
            self._correct(self.tracks[ti], people[pi], now)
        for pi, p in enumerate(people):
            if pi not in used_p:
                self.tracks.append(Track(self._next_id, p.x, p.y, height=p.height, standing=p.standing,
                                         last_seen=now, last_update=now))
                self._next_id += 1
        self.tracks = [t for t in self.tracks if now - t.last_seen <= self.coast_seconds]
        return self.confirmed()

    def _correct(self, t: Track, p: Person, now: float) -> None:
        dt = now - t.last_update
        px, py = t.predict(now)
        rx, ry = p.x - px, p.y - py
        t.x, t.y = px + self.alpha * rx, py + self.alpha * ry
        if dt > 1e-3:
            t.vx += self.beta * rx / dt
            t.vy += self.beta * ry / dt
            speed = t.speed
            if speed > self.max_speed:
                t.vx, t.vy = t.vx * self.max_speed / speed, t.vy * self.max_speed / speed
        if p.standing and not t.standing:
            t.stood_up_at = now
        t.standing, t.height = p.standing, p.height
        t.hits += 1
        t.last_seen = t.last_update = now

    def confirmed(self) -> list[Track]:
        return [t for t in self.tracks if t.hits >= self.confirm_hits]
