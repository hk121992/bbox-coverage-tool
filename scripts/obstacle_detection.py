#!/usr/bin/env python3
"""
obstacle_detection.py — Detect obstacles that conflict with locker placement.

Uses YOLOv8 nano for fast inference on SV images. Checks if the proposed
locker box overlaps with detected objects (people, cars, bicycles, benches,
fire hydrants, etc.).

The YOLO model doesn't detect lampposts, bollards, or doors — for those we
rely on the Opus analysis obstacle list + confidence scoring.

Returns overlap info and a recommended x-shift to avoid obstacles.
"""

from pathlib import Path
import numpy as np

# Lazy-load model
_model = None

# COCO classes that constitute obstacles for locker placement
OBSTACLE_CLASSES = {
    0: "person", 1: "bicycle", 2: "car", 3: "motorcycle",
    5: "bus", 7: "truck", 9: "traffic light", 10: "fire hydrant",
    11: "stop sign", 12: "parking meter", 13: "bench",
    56: "chair", 58: "potted plant", 60: "dining table",
}


def _load_model():
    """Load YOLOv8 nano model (cached after first call)."""
    global _model
    if _model is not None:
        return
    from ultralytics import YOLO
    _model = YOLO("yolov8n.pt")


def detect_obstacles(img_path, conf_threshold=0.3):
    """Detect obstacle objects in an image using YOLOv8.

    Args:
        img_path: Path to image file
        conf_threshold: Minimum confidence for detections

    Returns:
        list of dicts, each with:
            class_name: str
            confidence: float
            bbox: (x1, y1, x2, y2) in pixels
            center: (cx, cy) in pixels
    """
    _load_model()
    results = _model(str(img_path), verbose=False, conf=conf_threshold)

    obstacles = []
    for r in results:
        for b in r.boxes:
            cls_id = int(b.cls[0])
            if cls_id not in OBSTACLE_CLASSES:
                continue
            conf = float(b.conf[0])
            x1, y1, x2, y2 = b.xyxy[0].tolist()
            obstacles.append({
                "class_name": OBSTACLE_CLASSES[cls_id],
                "confidence": round(conf, 3),
                "bbox": (x1, y1, x2, y2),
                "center": ((x1 + x2) / 2, (y1 + y2) / 2),
            })

    return obstacles


def check_overlap(locker_bbox, obstacles, min_overlap_pct=0.05):
    """Check if the proposed locker box overlaps with any obstacles.

    Args:
        locker_bbox: (x1, y1, x2, y2) of the proposed locker box in pixels
        obstacles: list from detect_obstacles()
        min_overlap_pct: minimum overlap (as fraction of obstacle area) to count

    Returns:
        dict with:
            has_conflict: bool
            conflicting_obstacles: list of obstacle dicts with overlap_pct
            suggested_shift_px: int (positive = shift right, negative = shift left)
            clear_zones: list of (x_start, x_end) pixel ranges with no obstacles
    """
    lx1, ly1, lx2, ly2 = locker_bbox
    locker_w = lx2 - lx1

    conflicts = []
    for obs in obstacles:
        ox1, oy1, ox2, oy2 = obs["bbox"]

        # Compute intersection
        ix1 = max(lx1, ox1)
        iy1 = max(ly1, oy1)
        ix2 = min(lx2, ox2)
        iy2 = min(ly2, oy2)

        if ix1 >= ix2 or iy1 >= iy2:
            continue  # no overlap

        overlap_area = (ix2 - ix1) * (iy2 - iy1)
        obs_area = max(1, (ox2 - ox1) * (oy2 - oy1))
        overlap_pct = overlap_area / obs_area

        if overlap_pct >= min_overlap_pct:
            conflict = dict(obs)
            conflict["overlap_pct"] = round(overlap_pct, 3)
            conflicts.append(conflict)

    # Compute suggested shift to avoid conflicts
    suggested_shift = 0
    if conflicts:
        # Find center of mass of conflicting obstacles
        obs_centers_x = [c["center"][0] for c in conflicts]
        obs_com = np.mean(obs_centers_x)
        locker_cx = (lx1 + lx2) / 2

        # Shift away from obstacle center of mass
        if obs_com > locker_cx:
            suggested_shift = -int(locker_w * 0.3)  # shift left
        else:
            suggested_shift = int(locker_w * 0.3)  # shift right

    return {
        "has_conflict": len(conflicts) > 0,
        "conflicting_obstacles": conflicts,
        "n_conflicts": len(conflicts),
        "suggested_shift_px": suggested_shift,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python3 obstacle_detection.py <image_path>")
        sys.exit(1)

    obstacles = detect_obstacles(sys.argv[1])
    print(f"Detected {len(obstacles)} obstacles:")
    for o in obstacles:
        print(f"  {o['class_name']} ({o['confidence']:.2f}) at {o['bbox']}")
