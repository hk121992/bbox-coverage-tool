#!/usr/bin/env python3
"""
depth_utils.py — Monocular depth estimation for Street View images.

Uses MiDaS v2.1 Small for fast CPU/MPS inference (~90 FPS) to:
1. Detect the ground plane (actual y-coordinate instead of hardcoded 88%)
2. Estimate relative distance to the placement point
3. Validate locker placement depth consistency

The MiDaS depth map is *relative* (inverse depth), not metric.
We use it for ground-plane detection and relative distance comparison,
not for absolute metric measurements.
"""

import numpy as np
from pathlib import Path

# Lazy-load model to avoid startup cost
_model = None
_transform = None
_device = None


def _load_model():
    """Load MiDaS Small model (cached after first call)."""
    global _model, _transform, _device
    if _model is not None:
        return

    import torch

    _device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")

    _model = torch.hub.load("intel-isl/MiDaS", "MiDaS_small", trust_repo=True)
    _model = _model.to(_device).eval()

    midas_transforms = torch.hub.load("intel-isl/MiDaS", "transforms", trust_repo=True)
    _transform = midas_transforms.small_transform


def estimate_depth(img_path):
    """Run MiDaS depth estimation on an image.

    Args:
        img_path: Path to image file (800×600 SV image)

    Returns:
        dict with:
            depth_map: np.ndarray (H, W) — relative inverse depth (higher = closer)
            ground_y_pct: float — estimated ground level as fraction of image height
            depth_at_center: float — relative depth at image center bottom
            depth_stats: dict — min, max, mean, std of depth map
    """
    import torch
    from PIL import Image

    _load_model()

    img_pil = Image.open(img_path).convert("RGB")
    W, H = img_pil.size
    img_np = np.array(img_pil)

    # Run MiDaS
    input_batch = _transform(img_np).to(_device)
    with torch.no_grad():
        prediction = _model(input_batch)
        prediction = torch.nn.functional.interpolate(
            prediction.unsqueeze(1), size=(H, W),
            mode="bilinear", align_corners=False
        ).squeeze()

    depth = prediction.cpu().numpy()

    # ── Ground plane detection ──
    # MiDaS outputs inverse depth: higher = closer to camera.
    # Ground is close (high depth), buildings/sky are far (low depth).
    #
    # Strategy: The "locker placement ground level" is NOT the horizon line
    # (where ground first appears) but where the ground is at a plausible
    # locker distance (~3-8m). We use the 65th-75th depth percentile as
    # the target depth range, then find the y-level where row_mean crosses
    # into that range. This gives us the ground level at "placement distance"
    # rather than the horizon.

    row_means = np.mean(depth, axis=1)  # shape: (H,)

    d_min, d_max = row_means.min(), row_means.max()
    if d_max - d_min < 1e-6:
        ground_y_pct = 0.88  # fallback
    else:
        # Target: where depth is in the 60-70th percentile range
        # This corresponds to "medium-close" distance — where a locker would sit
        target_depth = np.percentile(row_means, 68)

        # Scan from top down to find where row mean first exceeds target
        ground_y_pct = 0.88  # default
        search_start = int(H * 0.55)
        search_end = int(H * 0.95)
        for row in range(search_start, search_end):
            if row_means[row] >= target_depth:
                ground_y_pct = row / H
                break

        # Sanity clamp: ground should be in bottom 40% of image
        ground_y_pct = max(0.65, min(0.93, ground_y_pct))

    # ── Depth at placement point ──
    # Sample depth at the estimated ground level, center of image
    ground_row = int(ground_y_pct * H)
    center_col = W // 2
    # Average a small patch around the point for robustness
    patch_h = max(1, H // 20)
    patch_w = max(1, W // 20)
    r1 = max(0, ground_row - patch_h)
    r2 = min(H, ground_row + patch_h)
    c1 = max(0, center_col - patch_w)
    c2 = min(W, center_col + patch_w)
    depth_at_ground = float(np.mean(depth[r1:r2, c1:c2]))

    return {
        "depth_map": depth,
        "ground_y_pct": round(ground_y_pct, 3),
        "depth_at_ground": round(depth_at_ground, 2),
        "depth_stats": {
            "min": round(float(depth.min()), 2),
            "max": round(float(depth.max()), 2),
            "mean": round(float(depth.mean()), 2),
            "std": round(float(depth.std()), 2),
        },
    }


def depth_at_point(depth_map, x_pct, y_pct, patch_size=10):
    """Sample depth at a specific image point (as percentages).

    Args:
        depth_map: np.ndarray (H, W) from estimate_depth()
        x_pct: horizontal position 0-1
        y_pct: vertical position 0-1
        patch_size: pixels to average around point

    Returns:
        float: relative depth value at the point
    """
    H, W = depth_map.shape
    cx = int(x_pct * W)
    cy = int(y_pct * H)
    r1, r2 = max(0, cy - patch_size), min(H, cy + patch_size)
    c1, c2 = max(0, cx - patch_size), min(W, cx + patch_size)
    return float(np.mean(depth_map[r1:r2, c1:c2]))


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python3 depth_utils.py <image_path>")
        sys.exit(1)

    result = estimate_depth(sys.argv[1])
    print(f"Ground level: {result['ground_y_pct']:.1%} of image height")
    print(f"Depth at ground: {result['depth_at_ground']}")
    print(f"Depth stats: {result['depth_stats']}")
