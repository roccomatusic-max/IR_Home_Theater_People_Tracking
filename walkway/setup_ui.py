"""Browser page for drawing walkway zones on a live top-down height map.

    python -m walkway config.yaml --setup     # then open http://<server>:8099

The page shows how tall things are from above (brighter = taller), never a
camera image. Drag on it to draw a zone, watch its live point count while you
walk through, and press Save to write the zones into config.yaml.
"""

from __future__ import annotations

import io
import json
import logging
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import yaml

from .zones import Zone, ZoneTracker

log = logging.getLogger(__name__)

PAGE = (Path(__file__).parent / "setup_page.html").read_text()


class Bounds:
    def __init__(self, x0: float, x1: float, y0: float, y1: float, cell: float = 0.02):
        self.x0, self.x1, self.y0, self.y1, self.cell = x0, x1, y0, y1, cell
        self.w = int((x1 - x0) / cell) + 1
        self.h = int((y1 - y0) / cell) + 1

    @classmethod
    def around(cls, points: np.ndarray, margin: float = 0.2) -> "Bounds":
        xs, ys = points[:, 0], points[:, 1]
        return cls(float(xs.min()) - margin, float(xs.max()) + margin, float(ys.min()) - margin, float(ys.max()) + margin)

    def as_dict(self) -> dict:
        return {"x0": self.x0, "x1": self.x1, "y0": self.y0, "y1": self.y1, "w": self.w, "h": self.h}


def height_png(points: np.ndarray, b: Bounds, max_h: float = 2.5) -> bytes:
    """Top-down max-height image, screen at the top. Heights only, no camera image."""
    from PIL import Image

    keep = (points[:, 0] >= b.x0) & (points[:, 0] <= b.x1) & (points[:, 1] >= b.y0) & (points[:, 1] <= b.y1)
    p = points[keep]
    grid = np.zeros((b.h, b.w), dtype=np.float32)
    col = ((p[:, 0] - b.x0) / b.cell).astype(int)
    row = ((b.y1 - p[:, 1]) / b.cell).astype(int)
    np.maximum.at(grid, (row, col), np.clip(p[:, 2], 0, max_h))
    buf = io.BytesIO()
    Image.fromarray((grid / max_h * 255).astype(np.uint8)).save(buf, format="PNG")
    return buf.getvalue()


def save_zones(config_path: str | Path, zones: list[Zone]) -> None:
    """Replace the zones list in config.yaml, keeping the other settings."""
    path = Path(config_path)
    raw = yaml.safe_load(path.read_text())
    raw["zones"] = [asdict(z) for z in zones]
    path.write_text(yaml.safe_dump(raw, sort_keys=False))


def zone_from_json(d: dict) -> Zone:
    z = Zone(
        name=str(d["name"]).strip().replace(" ", "_") or "zone",
        x_min=float(min(d["x_min"], d["x_max"])),
        x_max=float(max(d["x_min"], d["x_max"])),
        y_min=float(min(d["y_min"], d["y_max"])),
        y_max=float(max(d["y_min"], d["y_max"])),
        z_min=float(d.get("z_min", 1.0)),
        z_max=float(d.get("z_max", 2.1)),
        min_points=int(d.get("min_points", 150)),
    )
    return z


class SetupServer:
    """Holds the latest frame and serves the setup page in a background thread."""

    def __init__(self, tracker: ZoneTracker, config_path: str | Path, port: int = 8099, on_zones_changed=None):
        self.tracker = tracker
        self.on_zones_changed = on_zones_changed  # called with (old_names, new_names)
        self.config_path = config_path
        self.lock = threading.Lock()
        self.bounds: Bounds | None = None
        self.png = b""
        self.counts: dict[str, int] = {}
        self.version = 0
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep the console quiet
                pass

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/":
                    self._send(200, PAGE.encode(), "text/html; charset=utf-8")
                elif self.path.startswith("/map.png"):
                    with server.lock:
                        self._send(200, server.png, "image/png")
                elif self.path == "/state.json":
                    self._send(200, json.dumps(server.state()).encode(), "application/json")
                else:
                    self._send(404, b"not found", "text/plain")

            def do_POST(self):
                if self.path != "/zones":
                    return self._send(404, b"not found", "text/plain")
                try:
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    zones = [zone_from_json(d) for d in body["zones"]]
                    names = [z.name for z in zones]
                    if len(set(names)) != len(names):
                        raise ValueError("zone names must be unique")
                except Exception as e:  # bad input from the page
                    return self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
                server.set_zones(zones, save=bool(body.get("save")))
                self._send(200, json.dumps(server.state()).encode(), "application/json")

        self.httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        log.info("zone setup page on http://<this computer>:%d", port)

    def set_zones(self, zones: list[Zone], save: bool) -> None:
        with self.lock:
            old = [z.name for z in self.tracker.zones]
            self.tracker.set_zones(zones)
            if self.on_zones_changed:
                self.on_zones_changed(old, [z.name for z in zones])
            if save:
                save_zones(self.config_path, zones)
                log.info("saved %d zones to %s", len(zones), self.config_path)

    def update(self, points: np.ndarray, with_image: bool) -> None:
        with self.lock:
            if self.bounds is None:
                self.bounds = Bounds.around(points)
            self.counts = self.tracker.counts(points)
            if with_image:
                self.png = height_png(points, self.bounds)
                self.version += 1

    def state(self) -> dict:
        with self.lock:
            return {
                "version": self.version,
                "bounds": self.bounds.as_dict() if self.bounds else None,
                "zones": [asdict(z) for z in self.tracker.zones],
                "counts": self.counts,
                "occupied": {n: s.occupied for n, s in self.tracker.states.items()},
            }

    def close(self) -> None:
        self.httpd.shutdown()
