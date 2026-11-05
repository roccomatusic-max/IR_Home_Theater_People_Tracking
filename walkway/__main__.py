"""Run the walkway tracker.

    python -m walkway config.yaml               # normal operation, publishes to Home Assistant
    python -m walkway config.yaml --calibrate   # find the floor, save camera height/tilt/roll, exit
    python -m walkway config.yaml --learn-empty # learn the empty room so seated people are counted, exit
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
from .geometry import Mount, depth_to_camera_points, fit_floor
from .people import EmptyRoom
from .pipeline import Pipeline, make_zone_tracker

log = logging.getLogger("walkway")


def make_source(cfg: cfgmod.Config, sim: bool):
    if sim:
        from .sources import SimDepthSource

        src = SimDepthSource(cfg.mount)
        src.noise, src.dropout = 0.01, 0.05
        # Two rows of four seats, three people seated, one standing up next to the left walkway.
        for y in (0.6, -0.9):
            for x in (-1.2, -0.4, 0.4, 1.2):
                src.furniture += [(x, y, 0.7, 0.6, 0.5), (x, y - 0.38, 0.7, 0.15, 1.0)]
        src.people = [(-0.4, 0.6, 1.25), (0.4, 0.6, 1.25), (0.4, -0.9, 1.25), (-1.2, -0.9, 1.75)]
        return src
    from .sources import OakDepthSource

    return OakDepthSource(resolution=str(cfg.oak.get("resolution", "400p")), fps=int(cfg.oak.get("fps", 15)))


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
    ap.add_argument("--learn-empty", action="store_true", help="learn the empty room (nobody in it!), save, exit")
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
    pipe = Pipeline(source.intrinsics, cfg.mount, make_zone_tracker(cfg.zones, cfg), stride=cfg.stride)
    pipe.presence.empty_seconds = 60 * float(cfg.room.get("empty_minutes", 5))
    pipe.finder.empty_room = EmptyRoom.load(cfg.empty_map_path, mount_key(pipe.mount))
    tracker, lock = pipe.zones, pipe.lock  # main loop, setup page and MQTT switches all touch the tracker
    frames = iter(source.frames())

    if args.calibrate:
        calibrate(next(frames), source, cfg, args.config)
        return
    if args.learn_empty:
        learn_empty(pipe, frames, cfg, source=source)
        return
    if pipe.finder.empty_room is None:
        log.info("no empty-room map yet: walkways work, but only standing people are tracked. "
                 "Use 'Learn empty room' on the setup page (or --learn-empty) to count seated people too.")

    publisher = None
    if not args.tune and not args.heightmap and not args.sim:  # --sim never touches Home Assistant
        from .mqtt import Publisher

        def switch_changed(key: str, value: bool) -> None:
            if key != "detection":
                return  # the auto-off switch is used by Home Assistant automations
            with lock:
                changed = tracker.set_enabled(value)
            for name, occupied in changed.items():
                publisher.state(name, occupied)

        m = cfg.mqtt
        publisher = Publisher(
            m["host"], m.get("port", 1883), m.get("username"), m.get("password"),
            discovery_prefix=m.get("discovery_prefix", "homeassistant"), on_switch=switch_changed,
        )
        publisher.announce([z.name for z in cfg.zones])
        if pipe.room_mapped:
            publisher.announce_room()
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
        setup.camera = camera_dict(pipe.mount)
        setup.room_mapped = pipe.room_mapped

    last_print = last_map = 0.0
    for depth in frames:
        if setup and setup.calibrate_requested:
            try:
                pipe.set_mount(calibrate(depth, source, cfg, args.config))
                msg = "Auto-level: height {height} m, tilt {tilt_deg}°, roll {roll_deg}°. Saved.".format(**camera_dict(pipe.mount))
                msg += " Now press Learn empty room." if args.setup else ""
            except ValueError as e:
                msg = f"Auto-level failed: {e}"
            with lock:
                setup.calibrate_requested = False
                setup.message = msg
                setup.camera = camera_dict(pipe.mount)
                setup.room_mapped = pipe.room_mapped
                setup.bounds = None  # the floor plan moved; recompute the view
        if setup and setup.learn_requested:
            learn_empty(pipe, frames, cfg, first=depth, source=source)
            if publisher:
                publisher.announce_room()
            with lock:
                setup.learn_requested = False
                setup.room_mapped = True
                setup.message = "Learned the empty room. Seated people are now tracked too."
        if args.heightmap:
            save_heightmap(pipe.points(depth)[0], cfg, args.heightmap)
            return
        now = time.monotonic()
        r = pipe.process(depth, now)
        del depth  # nothing is kept between frames
        if publisher:
            for name, occupied in r.zone_changes.items():
                publisher.state(name, occupied)
            if pipe.room_mapped:
                publisher.room(len(r.tracks), pipe.presence.occupied)
        if setup:
            fresh_map = now - last_map >= 0.5
            setup.update(r.points, with_image=fresh_map, weights=r.weights, tracks=r.tracks, armed=r.armed,
                         people_count=len(r.tracks), room_occupied=pipe.presence.occupied)
            if fresh_map:
                last_map = now
        if args.tune and now - last_print >= 1.0:
            last_print = now
            with lock:
                counts = tracker.counts(r.points, r.weights)
                line = "  ".join(f"{n}={c} ({'ON' if tracker.states[n].occupied else 'off'})" for n, c in counts.items())
            log.info("%s  people=%d armed=%s", line, len(r.tracks), ",".join(r.armed) or "-")
        if args.sim:
            time.sleep(0.1)


def mount_key(m: Mount) -> tuple[float, ...]:
    return (m.x, m.y, m.height, m.tilt_deg, m.roll_deg, m.yaw_deg)


def learn_empty(pipe: Pipeline, frames, cfg: cfgmod.Config, first=None, n: int = 20, source=None) -> None:
    """Average a couple of seconds of frames of the empty room and save the height map."""
    sim_people = getattr(source, "people", None)
    if sim_people is not None:  # simulator: render the room without anyone in it
        saved, source.people = list(sim_people), []
        first = None
    batch = ([first] if first is not None else []) + [next(frames) for _ in range(n)]
    if sim_people is not None:
        source.people = saved
    room = pipe.learn_empty_room(batch)
    room.save(cfg.empty_map_path, mount_key(pipe.mount))
    log.info("learned the empty room (%d x %d cells) -> %s", room.grid.w, room.grid.h, cfg.empty_map_path)

if __name__ == "__main__":
    main()
