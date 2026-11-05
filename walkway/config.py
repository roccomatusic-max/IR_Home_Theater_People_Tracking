from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .geometry import Mount
from .zones import Zone


@dataclass
class Config:
    mount: Mount
    zones: list[Zone]
    on_frames: int
    off_seconds: float
    return_off_seconds: float
    stride: int
    mqtt: dict
    room: dict
    oak: dict
    path: Path

    @property
    def empty_map_path(self) -> Path:
        """Learned empty-room height map, stored next to config.yaml."""
        return self.path.parent / self.room.get("empty_map", "empty_room.npz")


def load(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text())
    t = raw.get("tracking", {}) or {}
    return Config(
        mount=Mount(**(raw.get("camera") or {})),
        zones=[Zone(**z) for z in (raw.get("zones") or [])],
        on_frames=t.get("on_frames", 3),
        off_seconds=t.get("off_seconds", 30.0),
        return_off_seconds=t.get("return_off_seconds", 5.0),
        stride=t.get("stride", 4),
        mqtt=raw.get("mqtt") or {},
        room=raw.get("room") or {},
        oak=raw.get("oak") or {},
        path=Path(path),
    )


def save_camera(path: str | Path, **values: float) -> None:
    """Update keys under `camera:` in config.yaml, keeping everything else."""
    p = Path(path)
    raw = yaml.safe_load(p.read_text())
    cam = raw.get("camera") or {}
    cam.update({k: round(float(v), 3) for k, v in values.items()})
    raw["camera"] = cam
    p.write_text(yaml.safe_dump(raw, sort_keys=False))
