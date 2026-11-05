"""Depth frame sources: the OAK-D Pro W camera, and a simulator for testing."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Protocol

import numpy as np

from .geometry import Intrinsics, Mount, camera_to_room

log = logging.getLogger(__name__)


class DepthSource(Protocol):
    intrinsics: Intrinsics

    def frames(self) -> Iterator[np.ndarray]: ...


class OakDepthSource:
    """Depth from a Luxonis OAK-D Pro W using only its two mono IR cameras.

    The color camera is never added to the pipeline, so no RGB image is ever
    captured. Depth frames live in memory and are dropped after processing.
    Written against the depthai 2.x API (pin depthai<3).

    Accuracy settings: high-accuracy stereo with left-right check (fewer
    phantom points), subpixel disparity (better at the far end of the room,
    which matters for a back-wall mount), and on-camera speckle, temporal,
    spatial and median filtering to steady the depth and fill small holes.
    """

    def __init__(self, resolution: str = "400p", fps: int = 15, ir_projector: float = 0.6, ir_flood: float = 0.3):
        import depthai as dai  # imported here so tests run without the SDK

        self._dai = dai
        res = {
            "400p": (dai.MonoCameraProperties.SensorResolution.THE_400_P, 640, 400),
            "800p": (dai.MonoCameraProperties.SensorResolution.THE_800_P, 1280, 800),
        }[resolution.lower()]
        p = dai.Pipeline()
        left = p.create(dai.node.MonoCamera)
        right = p.create(dai.node.MonoCamera)
        for cam, socket in ((left, dai.CameraBoardSocket.CAM_B), (right, dai.CameraBoardSocket.CAM_C)):
            cam.setResolution(res[0])
            cam.setBoardSocket(socket)
            cam.setFps(fps)
        stereo = p.create(dai.node.StereoDepth)
        stereo.setDefaultProfilePreset(dai.node.StereoDepth.PresetMode.HIGH_ACCURACY)
        stereo.setLeftRightCheck(True)
        stereo.setSubpixel(True)
        stereo.setDepthAlign(dai.CameraBoardSocket.CAM_C)
        self._configure_filters(stereo)
        left.out.link(stereo.left)
        right.out.link(stereo.right)
        xout = p.create(dai.node.XLinkOut)
        xout.setStreamName("depth")
        stereo.depth.link(xout.input)

        self._device = dai.Device(p)
        # The Pro's IR dot projector and flood light let it see in a dark room.
        self._device.setIrLaserDotProjectorIntensity(ir_projector)
        self._device.setIrFloodLightIntensity(ir_flood)
        calib = self._device.readCalibration()
        m = calib.getCameraIntrinsics(dai.CameraBoardSocket.CAM_C, res[1], res[2])
        self.intrinsics = Intrinsics(fx=m[0][0], fy=m[1][1], cx=m[0][2], cy=m[1][2])
        self._queue = self._device.getOutputQueue("depth", maxSize=2, blocking=False)

    def _configure_filters(self, stereo) -> None:
        dai = self._dai
        try:
            stereo.initialConfig.setMedianFilter(dai.MedianFilter.KERNEL_5x5)
            cfg = stereo.initialConfig.get()
            pp = cfg.postProcessing
            pp.speckleFilter.enable = True
            pp.speckleFilter.speckleRange = 50
            pp.temporalFilter.enable = True
            pp.spatialFilter.enable = True
            pp.spatialFilter.holeFillingRadius = 2
            pp.spatialFilter.numIterations = 1
            pp.thresholdFilter.minRange = 300  # mm
            pp.thresholdFilter.maxRange = 10000
            stereo.initialConfig.set(cfg)
        except (AttributeError, RuntimeError) as e:  # older/newer depthai without some of these
            log.warning("some depth filters unavailable in this depthai version: %s", e)

    def frames(self) -> Iterator[np.ndarray]:
        while True:
            yield self._queue.get().getFrame()  # uint16, millimetres

    def frames_with_timeout(self, timeout: float) -> Iterator[np.ndarray]:
        """Like frames(), but raises if the camera goes quiet for `timeout` seconds."""
        import time

        last = time.monotonic()
        while True:
            msg = self._queue.tryGet()
            if msg is not None:
                last = time.monotonic()
                yield msg.getFrame()
                continue
            if self._device.isClosed() or time.monotonic() - last > timeout:
                raise RuntimeError(f"no depth frame for {timeout:.0f} s")
            time.sleep(0.005)

    def close(self) -> None:
        self._device.close()


class SimDepthSource:
    """Renders synthetic depth frames of an empty floor plus box-shaped people.

    Used by the tests and by `--sim` to try the pipeline without hardware.
    Each person is (x, y, height) on the floor plan, ~0.45 m square.
    """

    def __init__(self, mount: Mount, width: int = 640, height: int = 400, hfov_deg: float = 127.0):
        fx = (width / 2) / np.tan(np.radians(hfov_deg / 2))
        self.intrinsics = Intrinsics(fx=fx, fy=fx, cx=width / 2, cy=height / 2)
        self.mount = mount
        self.width = width
        self.height = height
        self.people: list[tuple[float, float, float]] = []
        # Other boxes in the room, e.g. seats: (x, y, width, depth, height).
        self.furniture: list[tuple[float, float, float, float, float]] = []
        self.noise = 0.0  # relative depth noise, e.g. 0.01 = 1%
        self.dropout = 0.0  # share of pixels with no reading
        self._rng = np.random.default_rng(0)

    def render(self) -> np.ndarray:
        """Ray-cast each pixel against the floor and each person's top face."""
        h, w = self.height, self.width
        v, u = np.mgrid[0:h, 0:w].astype(np.float32)
        rays_cam = np.stack(
            [(u - self.intrinsics.cx) / self.intrinsics.fx, (v - self.intrinsics.cy) / self.intrinsics.fy, np.ones_like(u)],
            axis=-1,
        )
        rays = rays_cam @ camera_to_room(self.mount).T  # direction per pixel, cam z = 1
        oz = self.mount.height
        down = -rays[..., 2]
        depth = np.zeros((h, w), dtype=np.float32)
        with np.errstate(divide="ignore", invalid="ignore"):
            t_floor = np.where(down > 1e-6, oz / down, 0)
        depth[:] = t_floor
        half = 0.225
        origin = np.array([self.mount.x, self.mount.y, oz])
        boxes = [(px, py, 2 * half, 2 * half, ph) for px, py, ph in self.people] + list(self.furniture)
        for bx, by, bw, bd, bh in boxes:
            # Ray vs. axis-aligned box (slab method), so the sides of a person
            # far off to one side are seen, not just the top of the head.
            lo = np.array([bx - bw / 2, by - bd / 2, 0.0])
            hi = np.array([bx + bw / 2, by + bd / 2, bh])
            with np.errstate(divide="ignore", invalid="ignore"):
                inv = 1.0 / rays
                t1 = (lo - origin) * inv
                t2 = (hi - origin) * inv
            t_near = np.nanmax(np.minimum(t1, t2), axis=-1)
            t_far = np.nanmin(np.maximum(t1, t2), axis=-1)
            hit = (t_near <= t_far) & (t_near > 0) & (t_near < depth)
            depth[hit] = t_near[hit]
        if self.noise:
            depth *= 1 + self._rng.normal(0, self.noise, depth.shape).astype(np.float32)
        if self.dropout:
            depth[self._rng.random(depth.shape) < self.dropout] = 0
        return (np.clip(depth, 0, 65.0) * 1000).astype(np.uint16)  # millimetres, like the OAK

    def frames(self) -> Iterator[np.ndarray]:
        while True:
            yield self.render()
