"""Run the walkway tracker.

    python -m walkway config.yaml               # normal operation, publishes to Home Assistant
    python -m walkway config.yaml --tune        # print point counts per zone, no MQTT
    python -m walkway config.yaml --heightmap map.png   # save a top-down height map with zones drawn
    python -m walkway config.yaml --setup       # draw zones at http://<this computer>:8099 (also runs normally)
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


def save_heightmap(points: np.ndarray, cfg: cfgmod.Config, path: str) -> None:
    """Save one top-down height map with zone boxes outlined. Heights only, never a camera image."""
    import io

    from PIL import Image, ImageDraw

    from .setup_ui import Bounds, height_png

    b = Bounds.around(points)
    img = Image.open(io.BytesIO(height_png(points, b))).convert("RGB")
    draw = ImageDraw.Draw(img)
    for z in cfg.zones:
        box = [(z.x_min - b.x0) / b.cell, (b.y1 - z.y_max) / b.cell, (z.x_max - b.x0) / b.cell, (b.y1 - z.y_min) / b.cell]
        draw.rectangle(box, outline=(255, 160, 0), width=2)
        draw.text((box[0] + 4, box[1] + 4), z.name, fill=(255, 160, 0))
    img.save(path)
    log.info("saved %s (%.1f x %.1f m, screen at top)", path, b.x1 - b.x0, b.y1 - b.y0)


def main() -> None:
    ap = argparse.ArgumentParser(prog="walkway")
    ap.add_argument("config")
    ap.add_argument("--tune", action="store_true", help="print zone point counts instead of publishing")
    ap.add_argument("--heightmap", metavar="PNG", help="save one top-down height map and exit")
    ap.add_argument("--setup", action="store_true", help="serve a page for drawing zones in a browser")
    ap.add_argument("--port", type=int, default=8099, help="port for --setup (default 8099)")
    ap.add_argument("--sim", action="store_true", help="use a simulated camera")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    cfg = cfgmod.load(args.config)
    source = make_source(cfg, args.sim)
    tracker = ZoneTracker(cfg.zones, on_frames=cfg.on_frames, off_seconds=cfg.off_seconds)

    publisher = None
    if not args.tune and not args.heightmap and not args.sim:  # --sim never touches Home Assistant
        from .mqtt import Publisher

        m = cfg.mqtt
        publisher = Publisher(m["host"], m.get("port", 1883), m.get("username"), m.get("password"))
        publisher.announce([z.name for z in cfg.zones], m.get("discovery_prefix", "homeassistant"))
        for z in cfg.zones:
            publisher.state(z.name, False)

    setup = None
    if args.setup:
        from .setup_ui import SetupServer

        def zones_changed(old: list[str], new: list[str]) -> None:
            if not publisher:
                return
            prefix = cfg.mqtt.get("discovery_prefix", "homeassistant")
            for name in set(old) - set(new):
                publisher.forget(name, prefix)
            added = [n for n in new if n not in old]
            publisher.announce(added, prefix)
            for name in added:
                publisher.state(name, False)

        setup = SetupServer(tracker, args.config, args.port, on_zones_changed=zones_changed)

    last_print = last_map = 0.0
    for depth in source.frames():
        points = depth_to_room_points(depth, source.intrinsics, cfg.mount, stride=cfg.stride)
        del depth  # nothing is kept between frames
        if args.heightmap:
            save_heightmap(points, cfg, args.heightmap)
            return
        now = time.monotonic()
        if setup:
            with setup.lock:  # the setup page may swap zones from another thread
                changed = tracker.update(points, now)
        else:
            changed = tracker.update(points, now)
        if publisher:
            for name, occupied in changed.items():
                publisher.state(name, occupied)
        if setup:
            fresh_map = now - last_map >= 0.5
            setup.update(points, with_image=fresh_map)
            if fresh_map:
                last_map = now
            if args.sim:
                time.sleep(0.1)
        if args.tune and now - last_print >= 1.0:
            last_print = now
            counts = tracker.counts(points)
            log.info("  ".join(f"{n}={c} ({'ON' if tracker.states[n].occupied else 'off'})" for n, c in counts.items()))
            if args.sim:
                time.sleep(0.1)


if __name__ == "__main__":
    main()
