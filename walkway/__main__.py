"""Run the walkway tracker.

    python -m walkway config.yaml               # normal operation, publishes to Home Assistant
    python -m walkway config.yaml --calibrate   # find the floor, save camera height/tilt/roll, exit
    python -m walkway config.yaml --setup       # draw zones at http://<this computer>:8099 (also runs normally)
    python -m walkway config.yaml --tune        # print zone scores, no MQTT
    python -m walkway config.yaml --heightmap map.png   # save a top-down height map with zones drawn
    python -m walkway config.yaml --sim --setup # try it with a simulated camera, no hardware
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import threading
import time

import numpy as np

from . import config as cfgmod
from .geometry import Mount, depth_to_camera_points, depth_to_room_points, fit_floor
from .zones import ZoneTracker

log = logging.getLogger("walkway")


def make_source(cfg: cfgmod.Config, sim: bool):
    if sim:
        from .sources import SimDepthSource

        src = SimDepthSource(cfg.mount)
        if cfg.zones:
            zone = cfg.zones[0]
            src.people = [((zone.x_min + zone.x_max) / 2, (zone.y_min + zone.y_max) / 2, 1.7)]
        return src
    from .sources import OakDepthSource

    return OakDepthSource()


def calibrate(depth: np.ndarray, source, cfg: cfgmod.Config, config_path: str) -> Mount:
    """Fit the floor, save height/tilt/roll to config.yaml, return the new mount."""
    fit = fit_floor(depth_to_camera_points(depth, source.intrinsics, stride=2))
    if fit.inlier_share < 0.15:
        log.warning("only %.0f%% of what the camera sees looks like floor; check the result", fit.inlier_share * 100)
    cfgmod.save_camera(config_path, height=fit.height, tilt_deg=fit.tilt_deg, roll_deg=fit.roll_deg)
    log.info(
        "auto-level: height %.2f m, tilt %.1f deg, roll %.1f deg (floor is %.0f%% of the view) -> saved",
        fit.height, fit.tilt_deg, fit.roll_deg, fit.inlier_share * 100,
    )
    return dataclasses.replace(cfg.mount, height=fit.height, tilt_deg=fit.tilt_deg, roll_deg=fit.roll_deg)


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


def camera_dict(m: Mount) -> dict:
    return {
        "x": m.x, "y": m.y,
        "height": round(m.height, 2), "tilt_deg": round(m.tilt_deg, 1), "roll_deg": round(m.roll_deg, 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser(prog="walkway")
    ap.add_argument("config")
    ap.add_argument("--calibrate", action="store_true", help="find the floor, save camera height/tilt/roll, exit")
    ap.add_argument("--setup", action="store_true", help="serve a page for drawing zones in a browser")
    ap.add_argument("--port", type=int, default=8099, help="port for --setup (default 8099)")
    ap.add_argument("--tune", action="store_true", help="print zone scores instead of publishing")
    ap.add_argument("--heightmap", metavar="PNG", help="save one top-down height map and exit")
    ap.add_argument("--sim", action="store_true", help="use a simulated camera")
    ap.add_argument("--log", metavar="FILE", help="also write the log to this file (rotated at 1 MB)")
    args = ap.parse_args()
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log:
        from logging.handlers import RotatingFileHandler

        handlers.append(RotatingFileHandler(args.log, maxBytes=1_000_000, backupCount=3))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=handlers)

    cfg = cfgmod.load(args.config)
    source = make_source(cfg, args.sim)
    mount = cfg.mount
    lock = threading.RLock()  # main loop, setup page and MQTT switch all touch the tracker
    tracker = ZoneTracker(
        cfg.zones, on_frames=cfg.on_frames, off_seconds=cfg.off_seconds, return_off_seconds=cfg.return_off_seconds
    )

    if args.calibrate:
        calibrate(next(iter(source.frames())), source, cfg, args.config)
        return

    publisher = None
    if not args.tune and not args.heightmap and not args.sim:  # --sim never touches Home Assistant
        from .mqtt import Publisher

        def switch_changed(enabled: bool) -> None:
            with lock:
                changed = tracker.set_enabled(enabled)
            for name, occupied in changed.items():
                publisher.state(name, occupied)

        m = cfg.mqtt
        publisher = Publisher(
            m["host"], m.get("port", 1883), m.get("username"), m.get("password"),
            discovery_prefix=m.get("discovery_prefix", "homeassistant"), on_switch=switch_changed,
        )
        publisher.announce([z.name for z in cfg.zones])
        publisher.wait_for_restore()

    setup = None
    if args.setup:
        from .setup_ui import SetupServer

        def zones_changed(old: list[str], new: list[str]) -> None:
            if not publisher:
                return
            for name in set(old) - set(new):
                publisher.forget(name)
            publisher.announce([n for n in new if n not in old])

        setup = SetupServer(tracker, args.config, args.port, on_zones_changed=zones_changed, lock=lock)
        setup.camera = camera_dict(mount)

    last_print = last_map = 0.0
    for depth in source.frames():
        if setup and setup.calibrate_requested:
            try:
                mount = calibrate(depth, source, cfg, args.config)
                msg = "Auto-level: height {height} m, tilt {tilt_deg}°, roll {roll_deg}°. Saved.".format(**camera_dict(mount))
            except ValueError as e:
                msg = f"Auto-level failed: {e}"
            with lock:
                setup.calibrate_requested = False
                setup.message = msg
                setup.camera = camera_dict(mount)
                setup.bounds = None  # the floor plan moved; recompute the view
        points, weights = depth_to_room_points(depth, source.intrinsics, mount, stride=cfg.stride, with_weights=True)
        del depth  # nothing is kept between frames
        if args.heightmap:
            save_heightmap(points, cfg, args.heightmap)
            return
        now = time.monotonic()
        with lock:
            changed = tracker.update(points, now, weights)
        if publisher:
            for name, occupied in changed.items():
                publisher.state(name, occupied)
        if setup:
            fresh_map = now - last_map >= 0.5
            setup.update(points, with_image=fresh_map, weights=weights)
            if fresh_map:
                last_map = now
        if args.tune and now - last_print >= 1.0:
            last_print = now
            with lock:
                counts = tracker.counts(points, weights)
                line = "  ".join(f"{n}={c} ({'ON' if tracker.states[n].occupied else 'off'})" for n, c in counts.items())
            log.info(line)
        if args.sim:
            time.sleep(0.1)


if __name__ == "__main__":
    main()
