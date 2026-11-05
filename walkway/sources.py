"""Depth frame sources: the OAK-D Pro W camera, and a simulator for testing."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

import numpy as np

from .geometry import Intrinsics, Mount, camera_to_room


class DepthSource(Protocol):
    intrinsics: Intrinsics

    def frames(self) -> Iterator[np.ndarray]: ...


class OakDepthSource:
    """Depth from a Luxonis OAK-D Pro W using only its two mono IR cameras.

    The color camera is never added to the pipeline, so no RGB image is ever
    captured. Depth frames live in memory and are dropped after processing.
    Written against the depthai 2.x API (pin depthai<3).
    """

    def __init__(self, ir_projector: float = 0.6, ir_flood: float = 0.3, fps: int = 15):
        import depthai as dai  # imported here so tests run without the SDK

        self._dai = dai
        p = dai.Pipeline()
        left = p.create(dai.node.MonoCamera)
        right = p.create(dai.node.MonoCamera)
        for cam, socket in ((left, dai.CameraBoardSocket.CAM_B), (right, dai.CameraBoardSocket.CAM_C)):
            cam.setResolution(dai.MonoCameraProperties.SensorResolution.THE_400_P)
            cam.setBoardSocket(socket)
            cam.setFps(fps)
        stereo = p.create(dai.node.StereoDepth)
        stereo.setDefaultProfilePreset(dai.node.StereoDepth.PresetMode.HIGH_DENSITY)
        stereo.setLeftRightCheck(True)
        stereo.setDepthAlign(dai.CameraBoardSocket.CAM_C)
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
        w, h = 640, 400
        m = calib.getCameraIntrinsics(dai.CameraBoardSocket.CAM_C, w, h)
        self.intrinsics = Intrinsics(fx=m[0][0], fy=m[1][1], cx=m[0][2], cy=m[1][2])
        self._queue = self._device.getOutputQueue("depth", maxSize=2, blocking=False)

    def frames(self) -> Iterator[np.ndarray]:
        while True:
            yield self._queue.get().getFrame()  # uint16, millimetres


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
        for px, py, ph in self.people:
            # Ray vs. axis-aligned box (slab method), so the sides of a person
            # far off to one side are seen, not just the top of the head.
            lo = np.array([px - half, py - half, 0.0])
            hi = np.array([px + half, py + half, ph])
            with np.errstate(divide="ignore", invalid="ignore"):
                inv = 1.0 / rays
                t1 = (lo - origin) * inv
                t2 = (hi - origin) * inv
            t_near = np.nanmax(np.minimum(t1, t2), axis=-1)
            t_far = np.nanmin(np.maximum(t1, t2), axis=-1)
            hit = (t_near <= t_far) & (t_near > 0) & (t_near < depth)
            depth[hit] = t_near[hit]
        return (depth * 1000).astype(np.uint16)  # millimetres, like the OAK

    def frames(self) -> Iterator[np.ndarray]:
        while True:
            yield self.render()
