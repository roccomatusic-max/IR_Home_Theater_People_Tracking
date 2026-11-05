"""Turn a depth frame into 3D points in room coordinates, and find the floor.

Room frame (metres):
    x: across the room, positive toward the RIGHT walkway (facing the screen)
    y: along the room, positive toward the SCREEN
    z: up from the floor

Camera frame (OpenCV convention): x right in the image, y down in the image,
z out of the lens. With tilt 0 and yaw 0 the camera points straight down,
the top of the image faces the screen, and image-right is room-right.

Mounting on the back wall is the same model with a large tilt (typically
30-60 degrees): the lens leans toward the screen. roll covers a camera that
isn't quite level side to side.
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
    tilt_deg: float = 0.0  # 0 = straight down; positive leans the lens toward the screen
    yaw_deg: float = 0.0  # positive turns the image clockwise seen from above
    roll_deg: float = 0.0  # side-to-side lean about the lens axis


def camera_to_room(mount: Mount) -> np.ndarray:
    """3x3 rotation taking camera-frame vectors to room-frame vectors."""
    straight_down = np.array([[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]])
    t = np.radians(mount.tilt_deg)
    tilt = np.array([[1.0, 0.0, 0.0], [0.0, np.cos(t), -np.sin(t)], [0.0, np.sin(t), np.cos(t)]])
    a = np.radians(-mount.yaw_deg)
    yaw = np.array([[np.cos(a), -np.sin(a), 0.0], [np.sin(a), np.cos(a), 0.0], [0.0, 0.0, 1.0]])
    r = np.radians(mount.roll_deg)
    roll = np.array([[np.cos(r), -np.sin(r), 0.0], [np.sin(r), np.cos(r), 0.0], [0.0, 0.0, 1.0]])
    return yaw @ tilt @ straight_down @ roll


def depth_to_camera_points(
    depth: np.ndarray,
    intr: Intrinsics,
    depth_scale: float = 0.001,
    stride: int = 4,
    max_range_m: float = 10.0,
) -> np.ndarray:
    """(N, 3) camera-frame points. Zero depth means "no reading" and is dropped."""
    d = depth[::stride, ::stride].astype(np.float32) * depth_scale
    h, w = d.shape
    v, u = np.mgrid[0:h, 0:w]
    u = u * stride
    v = v * stride
    valid = (d > 0) & (d < max_range_m)
    z = d[valid]
    x = (u[valid] - intr.cx) * z / intr.fx
    y = (v[valid] - intr.cy) * z / intr.fy
    return np.stack([x, y, z], axis=1)


# A point's weight is the floor-plan area its pixel covers, relative to a
# pixel 2 m from the lens. Far-away people produce fewer pixels; weighting
# by range squared makes a person "score" about the same anywhere in the
# room, which matters for a back-wall camera looking down a long room.
REFERENCE_RANGE_M = 2.0


def depth_to_room_points(
    depth: np.ndarray,
    intr: Intrinsics,
    mount: Mount,
    depth_scale: float = 0.001,
    stride: int = 4,
    max_range_m: float = 10.0,
    with_weights: bool = False,
):
    """Room-frame points from a depth image: (N, 3), or ((N, 3), (N,)) with weights."""
    cam = depth_to_camera_points(depth, intr, depth_scale, stride, max_range_m)
    room = cam @ camera_to_room(mount).T
    room += np.array([mount.x, mount.y, mount.height])
    if not with_weights:
        return room
    weights = (cam[:, 2] / REFERENCE_RANGE_M) ** 2
    return room, weights.astype(np.float32)


@dataclass(frozen=True)
class FloorFit:
    height: float
    tilt_deg: float
    roll_deg: float
    inlier_share: float  # fraction of points on the floor plane


def fit_floor(cam_points: np.ndarray, iterations: int = 400, tolerance: float = 0.03, seed: int = 0) -> FloorFit:
    """Find the floor in camera-frame points and derive the camera's height, tilt and roll.

    RANSAC over planes. Walls and the screen are also big planes, so a
    candidate only counts as floor if it is below the camera (its "up"
    direction points back at the lens from underneath) and no steeper than
    80 degrees from the lens axis. Among those, the plane with the most
    points wins, then it is refined with a least-squares fit.
    """
    rng = np.random.default_rng(seed)
    pts = cam_points
    if len(pts) > 20000:
        pts = pts[rng.choice(len(pts), 20000, replace=False)]
    pts = np.ascontiguousarray(pts, dtype=np.float64)
    px, py, pz = pts[:, 0], pts[:, 1], pts[:, 2]

    def dist(n: np.ndarray, d: float) -> np.ndarray:
        # Written out rather than pts @ n: a 3-wide matmul in a tight loop is
        # much slower on some BLAS builds (thread start-up per call).
        return np.abs(px * n[0] + py * n[1] + pz * n[2] + d)

    best_n, best_d, best_count = None, 0.0, 0
    for _ in range(iterations):
        a, b, c = pts[rng.integers(0, len(pts), 3)]
        n = np.cross(b - a, c - a)
        norm = np.linalg.norm(n)
        if norm < 1e-9:
            continue
        n /= norm
        d = -float(n[0] * a[0] + n[1] * a[1] + n[2] * a[2])
        if d < 0:  # orient the normal toward the camera (origin)
            n, d = -n, -d
        up = n  # floor's up direction, in camera coordinates
        tilt = np.degrees(np.arccos(np.clip(-up[2], -1, 1)))
        # Must be seen from above: image "down" (+y) should not point up the floor normal.
        if tilt > 80 or d < 0.8 or up[1] > 0.2:
            continue
        count = int(np.count_nonzero(dist(n, d) < tolerance))
        if count > best_count:
            best_n, best_d, best_count = n, d, count
    if best_n is None:
        raise ValueError("could not find the floor; is enough of it in view?")
    inliers = pts[dist(best_n, best_d) < tolerance]
    centroid = inliers.mean(axis=0)
    q = inliers - centroid
    _, vecs = np.linalg.eigh(q.T @ q)  # 3x3 scatter matrix; smallest eigenvector = normal
    n = vecs[:, 0]
    d = -float(n[0] * centroid[0] + n[1] * centroid[1] + n[2] * centroid[2])
    if d < 0:
        n, d = -n, -d
    tilt = float(np.degrees(np.arccos(np.clip(-n[2], -1, 1))))
    s = np.sin(np.radians(tilt))
    roll = float(np.degrees(np.arctan2(-n[0], -n[1]))) if s > 1e-3 else 0.0
    return FloorFit(height=d, tilt_deg=tilt, roll_deg=roll, inlier_share=best_count / len(pts))
