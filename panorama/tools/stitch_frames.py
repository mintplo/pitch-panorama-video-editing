"""Preview a calibrated pair on a cylindrical panorama and reuse its remap grids."""

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass
class CameraMap:
    size: tuple[int, int]
    region: tuple[slice, slice]
    x: np.ndarray
    y: np.ndarray


@dataclass
class PanoramaMaps:
    cameras: tuple[CameraMap, CameraMap]
    size: tuple[int, int]
    left_weight: np.ndarray
    covered: np.ndarray


def calibration_matrices(camera):
    intrinsic = np.asarray(camera["K"], dtype=np.float32)
    rotation = np.asarray(camera["R"], dtype=np.float32)
    for matrix in (intrinsic, rotation):
        if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
            raise ValueError("K and R must be finite 3 by 3 matrices")
    if min(intrinsic[0, 0], intrinsic[1, 1]) <= 0 or not np.allclose(intrinsic[2], [0, 0, 1]):
        raise ValueError("K requires positive focal lengths and bottom row [0, 0, 1]")
    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=0.002) or np.linalg.det(rotation) <= 0:
        raise ValueError("R must be a rotation from camera to panorama coordinates")
    return intrinsic, rotation


def build_maps(left_size, right_size, calibration):
    """Sizes are (width, height) after each camera's orientation correction."""
    sizes = (left_size, right_size)
    matrices = [calibration_matrices(calibration[side]) for side in ("left", "right")]
    focal = float(np.mean([intrinsic[0, 0] for intrinsic, _ in matrices]))
    warper = cv2.PyRotationWarper("cylindrical", focal)
    grids = [warper.buildMaps(size, *params) for size, params in zip(sizes, matrices)]
    origin_x = min(rect[0] for rect, _, _ in grids)
    origin_y = min(rect[1] for rect, _, _ in grids)
    width = max(rect[0] + x.shape[1] for rect, x, _ in grids) - origin_x
    height = max(rect[1] + x.shape[0] for rect, x, _ in grids) - origin_y

    cameras = []
    masks = []
    for size, ((x0, y0, _, _), x, y) in zip(sizes, grids):
        top, left = y0 - origin_y, x0 - origin_x
        region = (slice(top, top + x.shape[0]), slice(left, left + x.shape[1]))
        mask = np.zeros((height, width), dtype=bool)
        mask[region] = (x >= 0) & (x <= size[0] - 1) & (y >= 0) & (y <= size[1] - 1)
        masks.append(mask)
        cameras.append(CameraMap(size, region, x, y))
    if not (masks[0] & masks[1]).any():
        raise ValueError("No overlapping pixels for this calibration")

    # Distance from each camera's valid border gives headroom for a soft seam.
    distances = []
    for mask in masks:
        padded = np.pad(mask.astype(np.uint8), 1)
        distances.append(cv2.distanceTransform(padded, cv2.DIST_L2, 3)[1:-1, 1:-1])
    covered = masks[0] | masks[1]
    total = distances[0] + distances[1]
    weight = np.divide(distances[0], total, out=np.zeros_like(total), where=covered)
    return PanoramaMaps(tuple(cameras), (width, height), weight, covered)


def blend_frames(left, right, maps):
    """Warp one synchronized pair of 8-bit BGR images onto the common canvas."""
    width, height = maps.size
    output = np.zeros((height, width, 3), dtype=np.float32)
    for frame, camera, weight in zip((left, right), maps.cameras, (maps.left_weight, 1 - maps.left_weight)):
        if frame is None or frame.dtype != np.uint8 or frame.shape != (camera.size[1], camera.size[0], 3):
            raise ValueError("Frame dimensions or format differ from the calibrated BGR input")
        warped = cv2.remap(frame, camera.x, camera.y, cv2.INTER_LINEAR)
        output[camera.region] += warped * weight[camera.region][:, :, None]
    output[~maps.covered] = 0
    return np.rint(output).clip(0, 255).astype(np.uint8)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("left", "right", "calibration", "output"):
        parser.add_argument(field, type=Path)
    args = parser.parse_args()
    if args.output.suffix.lower() != ".png":
        parser.error("output must be a new PNG file")
    try:
        calibration = json.loads(args.calibration.read_text())
        frames = [cv2.imread(str(p), cv2.IMREAD_COLOR) for p in (args.left, args.right)]
        if any(frame is None for frame in frames):
            raise ValueError("Could not decode both input frames")
        maps = build_maps(frames[0].shape[1::-1], frames[1].shape[1::-1], calibration)
        ok, encoded = cv2.imencode(".png", blend_frames(*frames, maps))
        if not ok:
            raise ValueError("PNG encoding failed")
        with args.output.open("xb") as stream:
            stream.write(encoded)
    except (KeyError, TypeError, ValueError, OSError, cv2.error) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
