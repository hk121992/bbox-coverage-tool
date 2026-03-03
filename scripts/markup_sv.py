#!/usr/bin/env python3
"""
markup_sv.py — Annotate Street View images with bpost bbox locker placement overlays.

v2.0: Conditional markup — only annotates candidates with Feasible or Marginal verdict.
Supports both v1 (per-viewpoint) and v2 (grouped) analysis formats.

Colour coding by largest viable locker size:
  Yellow  = Compact  (0.6 × 0.7 × 2.0m)
  Green   = Standard (1.2 × 0.7 × 2.0m)
  Blue    = Large    (2.4 × 0.7 × 2.0m)
  Purple  = XL       (4.8 × 0.7 × 2.0m)

Usage:
  python3 scripts/markup_sv.py <analysis_json> [--output-dir DIR] [--verdicts FILE]

Output:
  {output_dir}/sv_marked_{viewpoint_idx}_{side}.png
  {analysis_json_dir}/sv_report.json
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# Locker size variants — keep in sync with LOCKER_SIZES in local_analysis.py
LOCKER_SIZES = {
    "compact":  {"w": 0.6, "d": 0.7, "h": 2.0, "clearance": 1.2,
                 "color": (255, 200, 0),   "label": "COMPACT  0.6m"},
    "standard": {"w": 1.2, "d": 0.7, "h": 2.0, "clearance": 1.5,
                 "color": (0, 200, 80),    "label": "STANDARD 1.2m"},
    "large":    {"w": 2.4, "d": 0.7, "h": 2.0, "clearance": 1.5,
                 "color": (0, 150, 255),   "label": "LARGE    2.4m"},
    "xl":       {"w": 4.8, "d": 0.7, "h": 2.0, "clearance": 2.0,
                 "color": (180, 0, 255),   "label": "XL       4.8m+"},
}

def _detect_open_space(analysis):
    """Detect if a placement is in an open space (plaza, square, etc.).

    Uses a combination of signals from the Opus analysis:
    1. Notes field keywords (most reliable) — uses word-boundary matching
    2. Low frame_coverage_pct (<40%) indicating no dominant wall
    3. Open-space-related obstacles (café seating, market stalls, etc.)
    4. Footpath/width descriptions containing "plaza"/"open"

    Returns dict with:
        is_open_space: bool
        confidence: float 0-1 (how certain we are it's open space)
        reason: str (why we think it's open space)
    """
    import re

    notes = (analysis.get("notes", "") or "").lower()
    obstacles = [o.lower() for o in analysis.get("obstacles", [])]
    obstacles_str = " ".join(obstacles)
    frame_cov = analysis.get("frame_coverage_pct", 60)
    footpath = (analysis.get("footpath_width_estimate", "") or "").lower()

    signals = 0
    reasons = []

    # Signal 1: notes keywords — require word boundary or specific compound phrases
    # "open plaza", "open square", "open space" are strong indicators
    # "pedestrian zone", "esplanade", "piazza" are also strong
    # "Place des..." (French square name) needs special handling
    strong_phrases = [
        r"\bopen\s+plaza\b", r"\bopen\s+square\b", r"\bopen\s+space\b",
        r"\bopen\s+area\b", r"\bpedestrian\s+zone\b", r"\besplanade\b",
        r"\bpiazza\b", r"\bplein\b",
        r"\bcobblestone\s+square\b", r"\bcobblestone\s+plaza\b",
        r"\bpublic\s+square\b", r"\btown\s+square\b",
    ]
    for pattern in strong_phrases:
        if re.search(pattern, notes):
            signals += 2
            reasons.append(f"notes: '{pattern.strip(chr(92)).strip('b')}'")
            break
    else:
        # Weaker signal: standalone "plaza" or "square" (not in "Place des...")
        # but NOT matching "placement", "replace", "marketplace"
        if re.search(r"\bplaza\b", notes) and "marketplace" not in notes:
            signals += 1
            reasons.append("notes mention 'plaza'")
        elif re.search(r"\bsquare\b", notes) and "square meter" not in notes:
            signals += 1
            reasons.append("notes mention 'square'")

    # Signal 2: low frame coverage — strong indicator of no dominant wall
    if frame_cov is not None and frame_cov < 40:
        signals += 1
        reasons.append(f"low frame_coverage ({frame_cov}%)")

    # Signal 3: open-space obstacles (café seating, food stalls = outdoor gathering)
    open_obstacle_patterns = [
        r"outdoor\s+caf[ée]", r"caf[ée]\s+seating", r"outdoor\s+seating",
        r"food\s+kiosk", r"food\s+stall", r"food\s+trailer",
        r"\bmonument\b", r"\bfountain\b", r"\bbandstand\b",
    ]
    for pattern in open_obstacle_patterns:
        if re.search(pattern, obstacles_str) or re.search(pattern, notes):
            signals += 1
            reasons.append(f"open-space obstacle: {pattern}")
            break

    # Signal 4: footpath/width description
    if re.search(r"\b(plaza|open|square)\b", footpath):
        signals += 1
        reasons.append("width estimate mentions open space")

    is_open = signals >= 3  # require 3+ signals for open space classification
    conf = min(1.0, signals / 5.0)

    return {
        "is_open_space": is_open,
        "confidence": round(conf, 2),
        "reason": "; ".join(reasons) if reasons else "no open-space indicators",
    }


def _project_3d_box(analysis, W, H, fov=90):
    """Project a 3D locker box onto the image plane using a pinhole camera model.

    Uses known camera parameters (FOV, pitch, height ~2.5m) and estimated
    distance from frame_coverage/available_width to compute perspective-correct
    2D polygon vertices for front, top, and side faces.

    Returns dict with face polygons as lists of (x,y) tuples, or None if
    projection fails (points behind camera, out of bounds, etc.).
    """
    import math
    import numpy as np

    size_key = analysis.get("largest_viable_size", "none")
    if size_key not in LOCKER_SIZES:
        return None

    spec = LOCKER_SIZES[size_key]

    # ── Camera intrinsics ──
    img_fov = analysis.get("fov", fov)
    if img_fov is None:
        img_fov = fov
    fov_h = math.radians(max(30, min(150, img_fov)))
    fx = (W / 2.0) / math.tan(fov_h / 2.0)
    fy = fx  # square pixels
    cx_img, cy_img = W / 2.0, H / 2.0

    # Camera pitch (positive = looking down)
    pitch_deg = analysis.get("pitch", 5)
    if pitch_deg is None:
        pitch_deg = 5
    pitch = math.radians(max(0, min(30, pitch_deg)))

    cam_height = 2.5  # Google SV camera height (metres)

    # ── Estimate distance to the locker ──
    frame_coverage = analysis.get("frame_coverage_pct", 60) / 100.0
    frame_coverage = max(0.20, min(0.90, frame_coverage))
    avail_m = max(spec["w"] + spec["clearance"],
                  analysis.get("available_width_m", 3.0))
    # distance = available_width / (2 * frame_coverage * tan(fov/2))
    distance = avail_m / (2.0 * frame_coverage * math.tan(fov_h / 2.0))
    distance = max(2.0, min(30.0, distance))

    # ── Locker 3D dimensions ──
    lw = spec["w"]   # width (m)
    ld = spec["d"]   # depth (0.7m)
    lh = spec["h"]   # height (2.0m)

    # ── Horizontal offset from placement_x_pct ──
    x_pct = analysis.get("placement_x_pct", 50) / 100.0
    x_pct = max(0.05, min(0.95, x_pct))
    visible_width = 2.0 * distance * math.tan(fov_h / 2.0)
    x_offset = (x_pct - 0.5) * visible_width

    # ── 8 corners of the locker box ──
    # World frame: X=right, Y=forward (into scene), Z=up
    # Box base center at (x_offset, distance, 0), front face at Y - ld/2
    c = np.array([
        [x_offset - lw / 2, distance - ld / 2, 0],      # 0: front-left-bottom
        [x_offset + lw / 2, distance - ld / 2, 0],      # 1: front-right-bottom
        [x_offset + lw / 2, distance - ld / 2, lh],     # 2: front-right-top
        [x_offset - lw / 2, distance - ld / 2, lh],     # 3: front-left-top
        [x_offset - lw / 2, distance + ld / 2, 0],      # 4: back-left-bottom
        [x_offset + lw / 2, distance + ld / 2, 0],      # 5: back-right-bottom
        [x_offset + lw / 2, distance + ld / 2, lh],     # 6: back-right-top
        [x_offset - lw / 2, distance + ld / 2, lh],     # 7: back-left-top
    ])

    # ── World → Camera transform ──
    # Shift to camera origin at (0, 0, cam_height)
    c[:, 2] -= cam_height

    # Convert world (X,Y,Z) to camera (Xc = right, Yc = down, Zc = forward)
    cam = np.zeros_like(c)
    cam[:, 0] = c[:, 0]       # Xc = X_world
    cam[:, 1] = -c[:, 2]      # Yc = -Z_shifted (up→down)
    cam[:, 2] = c[:, 1]       # Zc = Y_world (forward)

    # Apply pitch rotation around Xc axis
    cos_p, sin_p = math.cos(pitch), math.sin(pitch)
    R = np.array([[1, 0, 0], [0, cos_p, -sin_p], [0, sin_p, cos_p]])
    cam = (R @ cam.T).T

    # ── Project to 2D ──
    z_vals = cam[:, 2]
    if np.any(z_vals <= 0.1):
        return None  # points behind camera

    px = fx * cam[:, 0] / z_vals + cx_img
    py = fy * cam[:, 1] / z_vals + cy_img
    pixels = np.column_stack([px, py])

    # Sanity check — all points should be somewhat near the image
    margin = max(W, H)
    if np.any(pixels < -margin) or np.any(pixels[:, 0] > W + margin) or np.any(pixels[:, 1] > H + margin):
        return None

    # ── Define visible faces ──
    front = pixels[[0, 1, 2, 3]]
    top = pixels[[3, 2, 6, 7]]

    # Side face: show left or right depending on camera offset
    if x_offset > 0:
        side = pixels[[0, 3, 7, 4]]  # left side visible
    else:
        side = pixels[[1, 2, 6, 5]]  # right side visible

    def to_tuples(arr):
        return [(float(arr[i, 0]), float(arr[i, 1])) for i in range(arr.shape[0])]

    return {
        "front": to_tuples(front),
        "top": to_tuples(top),
        "side": to_tuples(side),
        "pixels": pixels,
        "distance": distance,
        "spec": spec,
    }


def markup_sv_image(img_path, analysis, output_path, fov=90):
    """Draw locker footprint on a Street View image and save to output_path.

    Uses available_width_m and FOV from the analysis to calibrate pixel-per-metre
    scale with proper perspective correction. Draws a semi-transparent filled
    rectangle with dimension labels and a score badge.

    If placement_confidence < 0.4, draws a dashed outline instead of solid.
    If placement_confidence < 0.2, skips markup entirely.

    Returns True on success, False if size is 'none' or image cannot be opened.
    """
    import math
    from PIL import Image, ImageDraw, ImageFont

    size_key = analysis.get("largest_viable_size", "none")
    if size_key not in LOCKER_SIZES:
        return False

    # Confidence gating (Exp D)
    confidence = analysis.get("placement_confidence", 0.5)
    if confidence < 0.2:
        return False

    spec = LOCKER_SIZES[size_key]
    try:
        img = Image.open(img_path).convert("RGBA")
    except Exception as e:
        print(f"    markup: cannot open {img_path}: {e}")
        return False

    W, H = img.size  # typically 800 × 600

    x_pct   = max(0.05, min(0.95, analysis.get("placement_x_pct", 50) / 100.0))
    cx      = int(x_pct * W)

    # ── FOV-corrected scale calibration (Exp A0) ──────────────────────
    # The focal length in pixels determines the real pixel-per-meter ratio.
    # At FOV=90° (baseline): fx = W/2 / tan(45°) = W/2
    # At FOV=60° (tight):    fx = W/2 / tan(30°) = W/2 * 1.73  (objects 1.73× larger)
    # At FOV=120° (wide):    fx = W/2 / tan(60°) = W/2 * 0.577  (objects 0.58× smaller)
    img_fov = analysis.get("fov", fov)
    fov_rad = math.radians(max(30, min(150, img_fov)) / 2)
    # Correction factor relative to 90° baseline
    baseline_tan = math.tan(math.radians(45))  # = 1.0
    fov_correction = baseline_tan / math.tan(fov_rad)

    frame_coverage = analysis.get("frame_coverage_pct", 60) / 100.0
    frame_coverage = max(0.20, min(0.90, frame_coverage))
    avail_m  = max(spec["w"] + spec["clearance"], analysis.get("available_width_m", 3.0))
    avail_px = W * frame_coverage * fov_correction
    ppx_m    = avail_px / avail_m          # pixels per metre (FOV-corrected)
    lw       = int(spec["w"] * ppx_m)
    lh       = int(spec["h"] * ppx_m)

    # Clamp box to reasonable image proportions (max 85% width, max 90% height)
    lw = min(lw, int(W * 0.85))
    lh = min(lh, int(H * 0.90))

    # Ground level adjusted by camera pitch
    pitch = analysis.get("pitch", 10)
    # Higher pitch → ground appears higher in frame
    pitch_offset = (pitch - 5) * 0.005  # ~0.5% per degree above baseline pitch=5
    y_bot    = int(H * (0.88 - pitch_offset))
    y_bot    = max(int(H * 0.70), min(int(H * 0.95), y_bot))  # clamp
    y_top    = max(0, y_bot - lh)

    r, g, b = spec["color"]
    low_confidence = confidence < 0.4

    # Semi-transparent fill
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    odraw   = ImageDraw.Draw(overlay)

    box_left  = max(0, cx - lw // 2)
    box_right = min(W, cx + lw // 2)

    if low_confidence:
        # Dashed outline for uncertain placements
        fill_alpha = 40
        outline_alpha = 160
    else:
        fill_alpha = 70
        outline_alpha = 230

    odraw.rectangle(
        [box_left, y_top, box_right, y_bot],
        fill=(r, g, b, fill_alpha),
        outline=(r, g, b, outline_alpha),
        width=3 if not low_confidence else 2,
    )

    # Draw dashed effect for low confidence (dotted border overlay)
    if low_confidence:
        dash_len = 8
        for y in range(y_top, y_bot, dash_len * 2):
            ye = min(y + dash_len, y_bot)
            odraw.line([(box_left, y), (box_left, ye)], fill=(r, g, b, outline_alpha), width=2)
            odraw.line([(box_right, y), (box_right, ye)], fill=(r, g, b, outline_alpha), width=2)
        for x in range(box_left, box_right, dash_len * 2):
            xe = min(x + dash_len, box_right)
            odraw.line([(x, y_top), (xe, y_top)], fill=(r, g, b, outline_alpha), width=2)
            odraw.line([(x, y_bot), (xe, y_bot)], fill=(r, g, b, outline_alpha), width=2)

    img = Image.alpha_composite(img, overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    # Dimension labels
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    label_color = (r, g, b)
    draw.text((box_left, max(0, y_top - 18)), f"← {spec['w']}m →",
              fill=label_color, font=font)
    draw.text((min(W - 40, box_right + 4), (y_top + y_bot) // 2),
              f"{spec['h']}m", fill=label_color, font=font)

    # Score + size + confidence badge (top-left)
    score = analysis.get("placement_score", 0)
    conf_str = f"  conf={confidence:.0%}" if confidence < 0.6 else ""
    badge = f"Score {score}/10  {spec['label']}{conf_str}"
    badge_w = len(badge) * 7 + 12
    draw.rectangle([4, 4, badge_w, 26], fill=(0, 0, 0, 200))
    badge_color = (255, 200, 100) if low_confidence else (255, 255, 100)
    draw.text((8, 7), badge, fill=badge_color, font=font)

    # Low confidence warning
    if low_confidence:
        draw.text((8, 28), "? uncertain placement", fill=(255, 180, 80), font=font)

    # Notes (bottom bar)
    notes = analysis.get("notes", "")
    if notes:
        draw.rectangle([0, H - 24, W, H], fill=(0, 0, 0, 160))
        draw.text((6, H - 20), notes[:110], fill=(220, 220, 220), font=font)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path)
    return True


def markup_sv_image_3d(img_path, analysis, output_path, fov=90):
    """Draw a perspective-correct 3D locker box on a Street View image.

    Uses pinhole camera model with known FOV, estimated pitch, and camera
    height (2.5m) to project 3D box corners onto the image plane. Renders
    front face, top face, and visible side face as semi-transparent polygons
    with edge outlines, giving a realistic 3D box appearance.

    Falls back to markup_sv_image() (2D flat rectangle) if projection fails.

    Returns True on success, False if skipped.
    """
    import math
    from PIL import Image, ImageDraw, ImageFont

    size_key = analysis.get("largest_viable_size", "none")
    if size_key not in LOCKER_SIZES:
        return False

    confidence = analysis.get("placement_confidence", 0.5)
    if confidence < 0.2:
        return False

    spec = LOCKER_SIZES[size_key]
    try:
        img = Image.open(img_path).convert("RGBA")
    except Exception as e:
        print(f"    markup: cannot open {img_path}: {e}")
        return False

    W, H = img.size

    # ── Open space detection (Exp E) ──
    open_space = _detect_open_space(analysis)
    is_freestanding = open_space["is_open_space"]

    # ── 3D projection ──
    proj = _project_3d_box(analysis, W, H, fov=fov)
    if proj is None:
        # Fall back to 2D
        return markup_sv_image(img_path, analysis, output_path, fov=fov)

    front_pts = proj["front"]
    top_pts = proj["top"]
    side_pts = proj["side"]
    r, g, b = spec["color"]
    low_confidence = confidence < 0.4

    # For freestanding placements, use amber/orange colour to distinguish
    if is_freestanding:
        r, g, b = (255, 165, 0)  # amber

    # ── Obstacle detection (Exp C) ──
    obstacle_conflict = None
    try:
        from obstacle_detection import detect_obstacles, check_overlap
        # Compute bounding box of the front face for overlap check
        all_x = [p[0] for p in front_pts]
        all_y = [p[1] for p in front_pts]
        locker_bbox = (min(all_x), min(all_y), max(all_x), max(all_y))
        obstacles = detect_obstacles(img_path, conf_threshold=0.30)
        obstacle_conflict = check_overlap(locker_bbox, obstacles)
    except Exception:
        pass  # YOLO not available — skip obstacle check

    # ── Draw faces on overlay ──
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)

    fill_alpha = 40 if low_confidence else 65
    outline_alpha = 160 if low_confidence else 230
    outline_width = 2 if low_confidence else 3

    # Side face (darkest — furthest from viewer)
    side_dark = (max(0, r - 50), max(0, g - 50), max(0, b - 50))
    odraw.polygon(side_pts, fill=(*side_dark, fill_alpha))

    # Top face (medium shade — lit from above)
    top_bright = (min(255, r + 30), min(255, g + 30), min(255, b + 30))
    odraw.polygon(top_pts, fill=(*top_bright, fill_alpha + 10))

    # Front face (primary color — closest to viewer)
    odraw.polygon(front_pts, fill=(r, g, b, fill_alpha))

    # For freestanding: draw ground footprint circle around the base
    if is_freestanding:
        # Ground footprint = ellipse around the base of the box
        fl_bot = front_pts[0]
        fr_bot = front_pts[1]
        # Compute approximate ground center and radius
        base_cx = (fl_bot[0] + fr_bot[0]) / 2
        base_cy = (fl_bot[1] + fr_bot[1]) / 2
        base_radius_x = abs(fr_bot[0] - fl_bot[0]) * 0.8
        base_radius_y = base_radius_x * 0.3  # perspective-compressed vertically
        odraw.ellipse(
            [base_cx - base_radius_x, base_cy - base_radius_y,
             base_cx + base_radius_x, base_cy + base_radius_y],
            outline=(255, 165, 0, outline_alpha),
            width=2,
        )

    # ── Edge outlines ──
    outline_color = (r, g, b, outline_alpha)

    def draw_edges(pts, color, width):
        for i in range(len(pts)):
            p1 = pts[i]
            p2 = pts[(i + 1) % len(pts)]
            odraw.line([p1, p2], fill=color, width=width)

    # Draw dashed edges for low confidence, solid for normal
    if low_confidence:
        def draw_dashed_edges(pts, color, width, dash_len=8):
            for i in range(len(pts)):
                x1, y1 = pts[i]
                x2, y2 = pts[(i + 1) % len(pts)]
                dx, dy = x2 - x1, y2 - y1
                length = math.sqrt(dx * dx + dy * dy)
                if length < 1:
                    continue
                num_dashes = max(1, int(length / (dash_len * 2)))
                for d in range(num_dashes):
                    t1 = d * 2 * dash_len / length
                    t2 = min((d * 2 + 1) * dash_len / length, 1.0)
                    if t1 >= 1.0:
                        break
                    sx, sy = x1 + dx * t1, y1 + dy * t1
                    ex, ey = x1 + dx * t2, y1 + dy * t2
                    odraw.line([(sx, sy), (ex, ey)], fill=color, width=width)
        draw_dashed_edges(front_pts, outline_color, outline_width)
        draw_dashed_edges(top_pts, outline_color, outline_width)
        draw_dashed_edges(side_pts, outline_color, outline_width)
    else:
        draw_edges(front_pts, outline_color, outline_width)
        draw_edges(top_pts, outline_color, outline_width)
        draw_edges(side_pts, outline_color, outline_width)

    # ── Obstacle conflict markers (Exp C) ──
    if obstacle_conflict and obstacle_conflict["has_conflict"]:
        for obs in obstacle_conflict["conflicting_obstacles"]:
            ox1, oy1, ox2, oy2 = obs["bbox"]
            # Red semi-transparent overlay on obstacle
            odraw.rectangle([ox1, oy1, ox2, oy2],
                           fill=(255, 0, 0, 35),
                           outline=(255, 50, 50, 180), width=2)
            # Red X through the obstacle
            odraw.line([(ox1, oy1), (ox2, oy2)], fill=(255, 50, 50, 180), width=2)
            odraw.line([(ox2, oy1), (ox1, oy2)], fill=(255, 50, 50, 180), width=2)

    # Composite
    img = Image.alpha_composite(img, overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    # ── Obstacle conflict warning text ──
    if obstacle_conflict and obstacle_conflict["has_conflict"]:
        n = obstacle_conflict["n_conflicts"]
        obs_names = ", ".join(set(o["class_name"]
                                  for o in obstacle_conflict["conflicting_obstacles"]))
        warn_text = f"! {n} obstacle(s): {obs_names}"
        warn_w = len(warn_text) * 7 + 12
        draw.rectangle([W - warn_w - 4, 4, W - 4, 22], fill=(180, 0, 0, 200))
        draw.text((W - warn_w, 7), warn_text, fill=(255, 200, 200), font=font)

    # ── Dimension labels ──
    # Width label along front face bottom edge
    fl_bottom = front_pts[0]  # front-left-bottom
    fr_bottom = front_pts[1]  # front-right-bottom
    mid_x = (fl_bottom[0] + fr_bottom[0]) / 2
    mid_y = (fl_bottom[1] + fr_bottom[1]) / 2
    draw.text((int(mid_x) - 20, int(mid_y) + 4),
              f"← {spec['w']}m →", fill=(r, g, b), font=font)

    # Height label along front face right edge
    fr_top = front_pts[2]     # front-right-top
    mid_hy = (fr_bottom[1] + fr_top[1]) / 2
    draw.text((int(fr_bottom[0]) + 4, int(mid_hy)),
              f"{spec['h']}m", fill=(r, g, b), font=font)

    # Depth label along top face (if visible enough)
    top_tl = top_pts[0]  # front-left-top
    top_bl = top_pts[3]  # back-left-top
    depth_len = math.sqrt((top_tl[0] - top_bl[0])**2 + (top_tl[1] - top_bl[1])**2)
    if depth_len > 20:  # only show if large enough
        mid_dx = (top_tl[0] + top_bl[0]) / 2
        mid_dy = (top_tl[1] + top_bl[1]) / 2
        draw.text((int(mid_dx) - 10, int(mid_dy) - 8),
                  f"{spec['d']}m", fill=(*top_bright[:3],), font=font)

    # ── Score + size + confidence badge (top-left) ──
    score = analysis.get("placement_score", 0)
    conf_str = f"  conf={confidence:.0%}" if confidence < 0.6 else ""
    placement_type = "FREESTANDING" if is_freestanding else ""
    badge = f"Score {score}/10  {spec['label']}{conf_str}"
    badge_w = len(badge) * 7 + 12
    draw.rectangle([4, 4, badge_w, 26], fill=(0, 0, 0, 200))
    badge_color = (255, 200, 100) if low_confidence else (255, 255, 100)
    draw.text((8, 7), badge, fill=badge_color, font=font)

    # Placement type / confidence warning (line 2)
    line2_y = 28
    if is_freestanding:
        draw.text((8, line2_y), "FREESTANDING placement",
                  fill=(255, 165, 0), font=font)
        line2_y += 14
    if low_confidence:
        draw.text((8, line2_y), "? uncertain placement",
                  fill=(255, 180, 80), font=font)

    # Notes (bottom bar)
    notes = analysis.get("notes", "")
    if notes:
        draw.rectangle([0, H - 24, W, H], fill=(0, 0, 0, 160))
        draw.text((6, H - 20), notes[:110], fill=(220, 220, 220), font=font)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path)
    return True


def _should_markup(candidate, verdicts=None):
    """v2.0: Check if a candidate should get locker overlay markup.

    Only candidates with Feasible or Marginal verdict get markup.
    Rejects interior scenes and low-confidence placements.
    If verdicts dict is provided, check by candidate id.
    If no verdicts, fall back to checking analysis fields.
    """
    # Scene type gate — never mark up interior/covered scenes
    if candidate.get("sv_scene_type") == "interior":
        candidate["markup_skipped_reason"] = "interior scene"
        return False
    if not candidate.get("sv_is_viable_exterior", True):
        candidate["markup_skipped_reason"] = "not viable exterior"
        return False

    # Placement confidence gate — skip very uncertain placements
    if candidate.get("sv_placement_confidence", 0.5) < 0.3:
        candidate["markup_skipped_reason"] = "low placement confidence"
        return False

    # Also check analysis dict (for non-enriched pipeline paths)
    analysis = candidate.get("analysis", {})
    if analysis.get("scene_type") == "interior":
        candidate["markup_skipped_reason"] = "interior scene (analysis)"
        return False
    if not analysis.get("is_viable_exterior", True):
        candidate["markup_skipped_reason"] = "not viable exterior (analysis)"
        return False

    if verdicts:
        cid = str(candidate.get("id", candidate.get("viewpoint_idx", 0)))
        verdict = verdicts.get(cid, {}).get("verdict", "")
        return verdict.lower() in ("feasible", "marginal")

    # Fallback: check physical_feasibility if available
    pf = candidate.get("physical_feasibility", {})
    if pf:
        verdict = pf.get("verdict", "")
        return verdict.lower().startswith("feasible") or verdict.lower() == "marginal"

    # No verdict info — default to markup if analysis shows viable
    return (analysis.get("largest_viable_size", "none") != "none" and
            analysis.get("placement_score", 0) >= 5)


def _select_best_image(candidate, img_dir=None):
    """v2.0: Select the best image for markup from available captures.

    Priority: best_side standard > best_side tight > wide > any standard.
    Indoor images get heavy penalty. Within same priority tier, Opus's
    best_image_idx is used as a tiebreaker (not an override).

    Returns (img_path, side, img_info_dict) or (None, None, {}).
    The img_info_dict contains fov, capture_type, heading etc. from the
    selected image, enabling FOV-corrected markup.
    """
    analysis = candidate.get("analysis", {})
    best_side = analysis.get("best_side", "left")
    best_image_idx = analysis.get("best_image_idx")
    images = candidate.get("images", candidate.get("sv_images", []))

    # Normalize to list of dicts, preserving original index
    normalized = []
    for i, img in enumerate(images):
        if isinstance(img, dict):
            d = dict(img)
            d["_orig_idx"] = i
            normalized.append(d)
        elif isinstance(img, str):
            normalized.append({"path": img, "side": Path(img).stem.split("_")[-1],
                               "capture_type": "standard", "_orig_idx": i})

    # Sort by priority (indoor images penalised heavily, best_image_idx as tiebreaker)
    def priority(img_info):
        ct = img_info.get("capture_type", "standard")
        side = img_info.get("side", "")
        indoor_penalty = 10 if img_info.get("heuristic_indoor", False) else 0
        # Tiebreaker: prefer Opus's best_image_idx within same tier (0 = match, 1 = no match)
        idx_bonus = 0 if img_info.get("_orig_idx") == best_image_idx else 1
        # Best side standard = 0, best side tight = 2, wide = 4, others = 6
        # Multiply base by 2 to leave room for idx_bonus tiebreaker
        if best_side in side and ct == "standard":
            return (0 + indoor_penalty) * 10 + idx_bonus
        if best_side in side and ct == "tight":
            return (2 + indoor_penalty) * 10 + idx_bonus
        if ct == "wide":
            return (4 + indoor_penalty) * 10 + idx_bonus
        if ct == "standard":
            return (6 + indoor_penalty) * 10 + idx_bonus
        return (8 + indoor_penalty) * 10 + idx_bonus

    normalized.sort(key=priority)

    for img_info in normalized:
        path = Path(img_info["path"])
        if path.exists():
            return path, img_info.get("side", best_side), img_info

    return None, None, {}


def generate_sv_report(analysis_data, markup_dir, output_path, verdicts=None,
                       enriched_candidates=None):
    """Build sv_report.json — ranked top candidates with marked image paths.

    v2.0: Supports grouped analysis format and enriched candidate data.
    """
    sector = analysis_data.get("meta", {}).get("sector", "unknown")
    top = analysis_data.get("top_candidates", [])

    ranked = []
    for rank, vp in enumerate(top, 1):
        a = vp.get("analysis", {})
        size = a.get("largest_viable_size", "none")
        spec = LOCKER_SIZES.get(size, {})
        vidx = vp.get("viewpoint_idx", 0)
        best_side = a.get("best_side", "left")

        # Check if this candidate has enrichment data
        enriched = None
        if enriched_candidates:
            for ec in enriched_candidates:
                if ec.get("sv_viewpoint_idx") == vidx or ec.get("id") == rank:
                    enriched = ec
                    break

        marked = markup_dir / f"sv_marked_{vidx}_{best_side}.png"
        entry = {
            "rank":               rank,
            "viewpoint_idx":      vidx,
            "lat":                vp.get("lat"),
            "lng":                vp.get("lng"),
            "idw_score":          vp.get("idw_score"),
            "placement_score":    a.get("placement_score", 0),
            "confidence":         a.get("confidence", ""),
            "largest_viable_size": size,
            "locker_dims":        f"{spec.get('w','?')}m W × {spec.get('d','?')}m D × {spec.get('h','?')}m H" if spec else "n/a",
            "available_width_m":  a.get("available_width_m"),
            "surface":            a.get("surface"),
            "wall_available":     a.get("wall_available"),
            "obstacles":          a.get("obstacles", []),
            "notes":              a.get("notes", ""),
            "marked_image":       str(marked.relative_to(markup_dir.parent))
                                   if marked.exists() else None,
            "source_images":      vp.get("images", []),
        }

        # Add enrichment fields if available
        if enriched:
            entry["address"] = enriched.get("address", "")
            entry["verdict"] = enriched.get("physical_feasibility", {}).get("verdict", "")
            entry["commentary"] = enriched.get("commentary", "")
            entry["contact_details"] = enriched.get("contact_details", {})
            entry["location_context"] = enriched.get("location_context", {})

        ranked.append(entry)

    report = {
        "meta": {
            "sector":    sector,
            "version":   analysis_data.get("meta", {}).get("version", "2.0"),
            "model":     analysis_data.get("meta", {}).get("model"),
            "generated": datetime.utcnow().isoformat(),
            "n_candidates": len(ranked),
        },
        "top_candidates": ranked,
        "locker_sizes_reference": {
            k: {"w": v["w"], "d": v["d"], "h": v["h"], "clearance": v["clearance"]}
            for k, v in LOCKER_SIZES.items()
        },
        "full_analysis_path": str(output_path.parent / "sv_detail_analysis.json"),
    }

    with open(output_path, "w") as fh:
        json.dump(report, fh, indent=2)
    print(f"sv_report.json written → {output_path}")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("analysis_json", help="Path to sv_detail_analysis.json")
    parser.add_argument("--output-dir", default=None,
                        help="Directory for marked images (default: images/ sibling of JSON)")
    parser.add_argument("--verdicts", default=None,
                        help="Path to JSON with verdict data (from Opus assessment)")
    args = parser.parse_args()

    analysis_path = Path(args.analysis_json)
    if not analysis_path.exists():
        print(f"Error: {analysis_path} not found", file=sys.stderr)
        sys.exit(1)

    with open(analysis_path) as fh:
        data = json.load(fh)

    # Load verdicts if provided
    verdicts = None
    if args.verdicts and Path(args.verdicts).exists():
        with open(args.verdicts) as fh:
            verdicts = json.load(fh)

    markup_dir = Path(args.output_dir) if args.output_dir else analysis_path.parent / "images"
    markup_dir.mkdir(parents=True, exist_ok=True)

    top_candidates = data.get("top_candidates", [])
    print(f"Marking up {len(top_candidates)} top candidates from {analysis_path.name}")

    marked_count = 0
    skipped_verdict = 0
    for vp in top_candidates:
        a       = vp.get("analysis", {})
        vidx    = vp.get("viewpoint_idx", 0)
        best_s  = a.get("best_side", "left")

        # v2.0: Only mark Feasible/Marginal candidates
        if not _should_markup(vp, verdicts):
            skipped_verdict += 1
            continue

        # v2.0: Smart image selection (returns FOV from selected image)
        best_img, best_side, img_info = _select_best_image(vp)
        if best_img:
            img_path = best_img
            out_side = best_side
            img_fov = img_info.get("fov", 90)
        else:
            # Fallback to best_side from analysis
            img_candidates = [
                p for p in vp.get("images", [])
                if best_s in (Path(p).name if isinstance(p, str)
                              else Path(p.get("path", "")).name)
            ] or vp.get("images", [])

            if not img_candidates:
                print(f"  viewpoint {vidx}: no image found, skipping")
                continue

            first = img_candidates[0]
            img_path = Path(first) if isinstance(first, str) else Path(first.get("path", ""))
            out_side = best_s
            img_fov = first.get("fov", 90) if isinstance(first, dict) else 90

        out_path = markup_dir / f"sv_marked_{vidx}_{out_side}.png"
        # Use 3D perspective projection (falls back to 2D if numpy unavailable)
        ok = markup_sv_image_3d(img_path, a, out_path, fov=img_fov)
        if ok:
            marked_count += 1
            size = a.get("largest_viable_size", "none")
            print(f"  [{vidx}] score={a.get('placement_score',0)}/10 "
                  f"size={size} → {out_path.name}")

    print(f"\nMarked {marked_count}/{len(top_candidates)} images → {markup_dir}")
    if skipped_verdict:
        print(f"  ({skipped_verdict} skipped — not Feasible/Marginal)")

    # Generate sv_report.json
    report_path = analysis_path.parent / "sv_report.json"
    generate_sv_report(data, markup_dir, report_path)


if __name__ == "__main__":
    main()
