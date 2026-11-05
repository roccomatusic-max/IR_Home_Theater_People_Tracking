"""Run the walkway tracker.

    python -m walkway config.yaml               # normal operation, publishes to Home Assistant
    python -m walkway config.yaml --tune        # print point counts per zone, no MQTT
    python -m walkway config.yaml --heightmap map.png   # save a top-down height map with zones drawn
    python -m walkway config.yaml --sim --tune  # try it with a simulated person, no camera
"""

from __future__ import annotations

import argparse
import logging
import time

import numpy as np

from . import config as cfgmod
from .geometry import depth_to_room_points
from .zones import ZoneTracker

log = logging.getLogger("walkway")


def make_source(cfg: cfgmod.Config, sim: bool):
    if sim:
        from .sources import SimDepthSource

        src = SimDepthSource(cfg.mount)
        zone = cfg.zones[0]
        src.people = [((zone.x_min + zone.x_max) / 2, 0.0, 1.7)]  # someone standing in the first zone
        return src
    from .sources import OakDepthSource

    return OakDepthSource()


def save_heightmap(points: np.ndarray, cfg: cfgmod.Config, path: str, cell: float = 0.02) -> None:
    """Top-down picture of how tall things are, with zone boxes outlined.

    Brighter = taller. Contains only heights, never a camera image.
    """
    from PIL import Image, ImageDraw

    xs, ys = points[:, 0], points[:, 1]
    x0, x1 = float(xs.min()), float(xs.max())
    y0, y1 = float(ys.min()), float(ys.max())
    w = int((x1 - x0) / cell) + 1
    h = int((y1 - y0) / cell) + 1
    grid = np.zeros((h, w), dtype=np.float32)
    col = ((xs - x0) / cell).astype(int)
    row = ((y1 - ys) / cell).astype(int)  # screen at the top of the picture
    np.maximum.at(grid, (row, col), np.clip(points[:, 2], 0, 2.5))
    img = Image.fromarray((grid / 2.5 * 255).astype(np.uint8)).convert("RGB")
    draw = ImageDraw.Draw(img)
    for z in cfg.zones:
        box = [(z.x_min - x0) / cell, (y1 - z.y_max) / cell, (z.x_max - x0) / cell, (y1 - z.y_min) / cell]
        draw.rectangle(box, outline=(255, 160, 0), width=2)
        draw.text((box[0] + 4, box[1] + 4), z.name, fill=(255, 160, 0))
    img.save(path)
    log.info("saved %s (%.1f x %.1f m, screen at top)", path, x1 - x0, y1 - y0)


def main() -> None:
    ap = argparse.ArgumentParser(prog="walkway")
    ap.add_argument("config")
    ap.add_argument("--tune", action="store_true", help="print zone point counts instead of publishing")
    ap.add_argument("--heightmap", metavar="PNG", help="save one top-down height map and exit")
    ap.add_argument("--sim", action="store_true", help="use a simulated camera")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    cfg = cfgmod.load(args.config)
    source = make_source(cfg, args.sim)
    tracker = ZoneTracker(cfg.zones, on_frames=cfg.on_frames, off_seconds=cfg.off_seconds)

    publisher = None
    if not args.tune and not args.heightmap:
        from .mqtt import Publisher

        m = cfg.mqtt
        publisher = Publisher(m["host"], m.get("port", 1883), m.get("username"), m.get("password"))
        publisher.announce([z.name for z in cfg.zones], m.get("discovery_prefix", "homeassistant"))
        for z in cfg.zones:
            publisher.state(z.name, False)

    last_print = 0.0
    for depth in source.frames():
        points = depth_to_room_points(depth, source.intrinsics, cfg.mount, stride=cfg.stride)
        del depth  # nothing is kept between frames
        if args.heightmap:
            save_heightmap(points, cfg, args.heightmap)
            return
        now = time.monotonic()
        changed = tracker.update(points, now)
        if publisher:
            for name, occupied in changed.items():
                publisher.state(name, occupied)
        if args.tune and now - last_print >= 1.0:
            last_print = now
            counts = tracker.counts(points)
            log.info("  ".join(f"{n}={c} ({'ON' if tracker.states[n].occupied else 'off'})" for n, c in counts.items()))
            if args.sim:
                time.sleep(0.1)


if __name__ == "__main__":
    main()
