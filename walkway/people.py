"""Whole-room people tracking from a top-down height grid.

Everyone in view is found as a "blob" of floor-plan cells that are taller
than the empty room. Seated people are counted too, as long as an empty-room
map has been learned. Without one, only standing people are found, because
seat backs would look like people.

Nothing here identifies anyone. A person is a position, a height and whether
they're standing.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

CELL = 0.10  # metres per grid cell


@dataclass(frozen=True)
class Grid:
    x0: float
    y0: float
    w: int
    h: int
    cell: float = CELL

    @classmethod
    def covering(cls, points: np.ndarray, margin: float = 0.3) -> "Grid":
        low = points[points[:, 2] < 2.3]
        if len(low) < 100:
            low = points
        x0, x1 = np.percentile(low[:, 0], [0.5, 99.5])
        y0, y1 = np.percentile(low[:, 1], [0.5, 99.5])
        x0, y0 = x0 - margin, y0 - margin
        return cls(float(x0), float(y0), int((x1 + margin - x0) / CELL) + 1, int((y1 + margin - y0) / CELL) + 1)

    def heights(self, points: np.ndarray, max_h: float = 2.3) -> np.ndarray:
        """Max height per cell (0 where nothing was seen). Row 0 = smallest y."""
        col = ((points[:, 0] - self.x0) / self.cell).astype(np.int64)
        row = ((points[:, 1] - self.y0) / self.cell).astype(np.int64)
        ok = (col >= 0) & (col < self.w) & (row >= 0) & (row < self.h) & (points[:, 2] < max_h)
        g = np.zeros((self.h, self.w), dtype=np.float32)
        np.maximum.at(g, (row[ok], col[ok]), points[ok, 2].astype(np.float32))
        return g

    def centre(self, row: float, col: float) -> tuple[float, float]:
        return self.x0 + (col + 0.5) * self.cell, self.y0 + (row + 0.5) * self.cell


@dataclass
class Person:
    x: float
    y: float
    height: float  # tallest point (m)
    area: float  # m² seen from above
    standing: bool


class EmptyRoom:
    """The learned height map of the room with nobody in it."""

    def __init__(self, grid: Grid, heights: np.ndarray):
        self.grid = grid
        self.heights = heights

    @classmethod
    def learn(cls, frames_of_points: list[np.ndarray]) -> "EmptyRoom":
        grid = Grid.covering(frames_of_points[0])
        stack = np.stack([grid.heights(p) for p in frames_of_points])
        return cls(grid, np.median(stack, axis=0))

    def save(self, path: str | Path, mount_key: tuple[float, ...] = ()) -> None:
        """Save heights only (a coarse 10 cm grid of numbers, no image), plus the
        camera mount it was learned with so a moved camera invalidates it."""
        g = self.grid
        np.savez_compressed(path, heights=self.heights, x0=g.x0, y0=g.y0, w=g.w, h=g.h, cell=g.cell,
                            mount=np.array(mount_key, dtype=np.float64))

    @classmethod
    def load(cls, path: str | Path, mount_key: tuple[float, ...] = ()) -> "EmptyRoom | None":
        """The saved map, or None if there isn't one or the camera has moved since."""
        p = Path(path)
        if not p.exists():
            return None
        d = np.load(p)
        saved = tuple(float(v) for v in d["mount"]) if "mount" in d else ()
        if mount_key and (len(saved) != len(mount_key) or max(abs(a - b) for a, b in zip(saved, mount_key)) > 0.05):
            return None
        grid = Grid(float(d["x0"]), float(d["y0"]), int(d["w"]), int(d["h"]), float(d["cell"]))
        return cls(grid, d["heights"])


def _blobs(mask: np.ndarray) -> list[list[tuple[int, int]]]:
    """8-connected groups of True cells."""
    seen = np.zeros_like(mask, dtype=bool)
    out = []
    h, w = mask.shape
    for r0, c0 in zip(*np.nonzero(mask)):
        if seen[r0, c0]:
            continue
        seen[r0, c0] = True
        cells, q = [], deque([(r0, c0)])
        while q:
            r, c = q.popleft()
            cells.append((r, c))
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    rr, cc = r + dr, c + dc
                    if 0 <= rr < h and 0 <= cc < w and mask[rr, cc] and not seen[rr, cc]:
                        seen[rr, cc] = True
                        q.append((rr, cc))
        out.append(cells)
    return out


def _merge_close(people: list[Person], distance: float) -> list[Person]:
    """Join detections whose centres are closer than one person's width."""
    people = sorted(people, key=lambda p: -p.area)
    out: list[Person] = []
    for p in people:
        for q in out:
            if ((p.x - q.x) ** 2 + (p.y - q.y) ** 2) ** 0.5 < distance:
                w = q.area + p.area
                q.x, q.y = (q.x * q.area + p.x * p.area) / w, (q.y * q.area + p.y * p.area) / w
                q.height, q.area = max(q.height, p.height), w
                q.standing = q.standing or p.standing
                break
        else:
            out.append(p)
    return out


@dataclass
class PeopleFinder:
    empty_room: EmptyRoom | None = None
    standing_height: float = 1.45  # tallest point above this = standing
    above_room: float = 0.25  # how much taller than the empty room a cell must be
    min_area: float = 0.04  # m²; smaller blobs are noise or a pet's head
    min_person_height: float = 0.7  # ignore pets, bags, feet on the floor
    merge_distance: float = 0.4  # two blobs closer than this (m) are one person

    def find(self, points: np.ndarray, grid: Grid | None = None) -> list[Person]:
        if self.empty_room is not None:
            grid = self.empty_room.grid
            cur = grid.heights(points)
            mask = (cur - self.empty_room.heights > self.above_room) & (cur > self.min_person_height)
        else:
            grid = grid or Grid.covering(points)
            cur = grid.heights(points)
            mask = cur > self.standing_height  # no empty-room map: only standing people
        people = []
        cell_area = grid.cell * grid.cell
        # Far from the camera points get sparse and one person can show up as
        # a few patches; _merge_close joins patches closer than a person's width.
        # (Growing the mask instead would glue neighbours in adjacent seats.)
        for cells in _blobs(mask):
            area = len(cells) * cell_area
            if area < self.min_area:
                continue
            rows = np.array([c[0] for c in cells])
            cols = np.array([c[1] for c in cells])
            hs = cur[rows, cols]
            top = float(hs.max())
            # Centre on the head and shoulders: arms, legs and a leaning body
            # spread a blob out, but the top of the person is where they are.
            tall = hs >= top - 0.3
            x, y = grid.centre(float(rows[tall].mean()), float(cols[tall].mean()))
            people.append(Person(x=x, y=y, height=top, area=area, standing=top >= self.standing_height))
        return _merge_close(people, self.merge_distance)
