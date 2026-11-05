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
    stride: int
    mqtt: dict


def load(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text())
    t = raw.get("tracking", {})
    return Config(
        mount=Mount(**raw.get("camera", {})),
        zones=[Zone(**z) for z in raw["zones"]],
        on_frames=t.get("on_frames", 3),
        off_seconds=t.get("off_seconds", 30.0),
        stride=t.get("stride", 4),
        mqtt=raw.get("mqtt", {}),
    )
