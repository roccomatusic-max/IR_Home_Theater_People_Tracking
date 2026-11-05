"""Turn a depth frame into 3D points in room coordinates.

Room frame (metres):
    x: across the room, positive toward the RIGHT walkway (facing the screen)
    y: along the room, positive toward the SCREEN
    z: up from the floor

Camera frame (OpenCV convention): x right in the image, y down in the image,
z out of the lens. With tilt 0 and yaw 0 the camera points straight down,
the top of the image faces the screen, and image-right is room-right.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Intrinsics:
    fx: float
    fy: float
    cx: float
    cy: float


@dataclass(frozen=True)
class Mount:
    x: float = 0.0  # camera position on the floor plan (m)
    y: float = 0.0
    height: float = 2.5  # lens height above the floor (m)
    tilt_deg: float = 0.0  # positive tips the lens toward the screen
    yaw_deg: float = 0.0  # positive turns the image clockwise seen from above


def camera_to_room(mount: Mount) -> np.ndarray:
    """3x3 rotation taking camera-frame vectors to room-frame vectors."""
    straight_down = np.array([[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]])
    t = np.radians(mount.tilt_deg)
    tilt = np.array([[1.0, 0.0, 0.0], [0.0, np.cos(t), -np.sin(t)], [0.0, np.sin(t), np.cos(t)]])
    a = np.radians(-mount.yaw_deg)
    yaw = np.array([[np.cos(a), -np.sin(a), 0.0], [np.sin(a), np.cos(a), 0.0], [0.0, 0.0, 1.0]])
    return yaw @ tilt @ straight_down


def depth_to_room_points(
    depth: np.ndarray,
    intr: Intrinsics,
    mount: Mount,
    depth_scale: float = 0.001,
    stride: int = 4,
    max_range_m: float = 8.0,
) -> np.ndarray:
    """Return an (N, 3) array of room-frame points from a depth image.

    depth is an HxW array of raw depth values (millimetres for OAK cameras,
    hence depth_scale=0.001). Zero means "no reading" and is dropped.
    stride subsamples pixels for speed.
    """
    d = depth[::stride, ::stride].astype(np.float32) * depth_scale
    h, w = d.shape
    v, u = np.mgrid[0:h, 0:w]
    u = u * stride
    v = v * stride
    valid = (d > 0) & (d < max_range_m)
    z = d[valid]
    x = (u[valid] - intr.cx) * z / intr.fx
    y = (v[valid] - intr.cy) * z / intr.fy
    cam = np.stack([x, y, z], axis=1)
    room = cam @ camera_to_room(mount).T
    room += np.array([mount.x, mount.y, mount.height])
    return room
