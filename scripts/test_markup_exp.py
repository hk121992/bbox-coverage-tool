#!/usr/bin/env python3
"""
Test script for Experiments A0 (FOV correction) and D (confidence gating).

Generates before/after PNGs for 3 problem candidates from the Liège walkthrough:
- VP 493 (Rank 1): Recommended candidate, cobblestone/bollards, FOV=90
- VP 784 (Rank 16): Doors/windows shopping alley, FOV=90, confidence=0.45
- VP 1059 (Rank 10): Open plaza Place des Prés, FOV=90 + FOV=60 tight

Saves output to data/local_reports/Liège_0.5km_20260302/test_markup_exp/
"""

import json
import shutil
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "scripts"))

from markup_sv import markup_sv_image, markup_sv_image_3d

REPORT_DIR = project_root / "data" / "local_reports" / "Liège_0.5km_20260302"
IMAGES_DIR = REPORT_DIR / "images"
OUTPUT_DIR = REPORT_DIR / "test_markup_exp"


def setup():
    """Create output dirs."""
    (OUTPUT_DIR / "before").mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "after").mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "after_3d").mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "after_fov60").mkdir(parents=True, exist_ok=True)


def load_analysis():
    """Load sv_detail_analysis.json and return dict keyed by viewpoint_idx."""
    with open(REPORT_DIR / "sv_detail_analysis.json") as f:
        data = json.load(f)
    by_vp = {}
    for c in data.get("top_candidates", []):
        by_vp[c.get("viewpoint_idx", 0)] = c
    return by_vp


def copy_existing_marked(vp_idx, side):
    """Copy the existing marked image to the 'before' dir."""
    src = IMAGES_DIR / f"sv_marked_{vp_idx}_{side}.png"
    dst = OUTPUT_DIR / "before" / f"sv_marked_{vp_idx}_{side}.png"
    if src.exists():
        shutil.copy2(src, dst)
        print(f"  [before] copied {src.name}")
        return True
    else:
        print(f"  [before] {src.name} does not exist — no existing markup")
        return False


def run_markup(source_img, analysis, output_path, fov=90, label=""):
    """Run markup_sv_image and report result."""
    ok = markup_sv_image(source_img, analysis, output_path, fov=fov)
    fname = output_path.name
    if ok:
        print(f"  [after{label}] generated {fname}")
    else:
        print(f"  [after{label}] SKIPPED {fname} (returned False)")
    return ok


def test_candidate(vp_idx, analysis_data, source_img_name, side, extra_tests=None):
    """Run before/after comparison for one candidate."""
    candidate = analysis_data.get(vp_idx)
    if not candidate:
        print(f"\n❌ VP {vp_idx} not found in analysis data")
        return

    a = candidate.get("analysis", {})
    imgs = candidate.get("images", [])

    print(f"\n{'='*60}")
    print(f"VP {vp_idx} — score={a.get('placement_score')}/10, "
          f"size={a.get('largest_viable_size')}, "
          f"confidence={a.get('placement_confidence')}, "
          f"frame_coverage={a.get('frame_coverage_pct')}%")
    print(f"  placement_x_pct={a.get('placement_x_pct')}, "
          f"available_width_m={a.get('available_width_m')}, "
          f"best_side={a.get('best_side')}, best_image_idx={a.get('best_image_idx')}")
    print(f"  obstacles: {a.get('obstacles', [])}")
    print(f"{'='*60}")

    # Copy existing marked image
    copy_existing_marked(vp_idx, side)

    # Find source image
    source_img = IMAGES_DIR / source_img_name
    if not source_img.exists():
        print(f"  ❌ Source image {source_img_name} not found!")
        return

    # Run new 2D markup (FOV=90 default)
    out_path = OUTPUT_DIR / "after" / f"sv_marked_{vp_idx}_{side}.png"
    run_markup(source_img, a, out_path, fov=90)

    # Run 3D perspective markup
    out_path_3d = OUTPUT_DIR / "after_3d" / f"sv_marked_{vp_idx}_{side}.png"
    ok_3d = markup_sv_image_3d(source_img, a, out_path_3d, fov=90)
    if ok_3d:
        print(f"  [3D] generated {out_path_3d.name}")
    else:
        print(f"  [3D] SKIPPED or fell back to 2D")

    # Extra test: FOV=60 tight image
    if extra_tests:
        for test in extra_tests:
            test_img = IMAGES_DIR / test["img"]
            if test_img.exists():
                out_path_fov = OUTPUT_DIR / "after_fov60" / f"sv_marked_{vp_idx}_{test['label']}.png"
                # Patch analysis with the tight image's FOV
                a_copy = dict(a)
                a_copy["fov"] = test["fov"]
                run_markup(test_img, a_copy, out_path_fov, fov=test["fov"],
                          label=f" fov={test['fov']}")
            else:
                print(f"  ❌ Extra test image {test['img']} not found")


def test_confidence_edge_cases(analysis_data):
    """Test confidence gating thresholds."""
    print(f"\n{'='*60}")
    print("Confidence gating edge cases")
    print(f"{'='*60}")

    # Use VP 493 as base but override confidence
    vp = analysis_data.get(493)
    if not vp:
        print("  ❌ VP 493 not found")
        return

    a_base = dict(vp.get("analysis", {}))
    source_img = IMAGES_DIR / "detail_sv_696_left.png"
    if not source_img.exists():
        print(f"  ❌ source image not found")
        return

    for conf in [0.15, 0.25, 0.35, 0.50, 0.70, 0.90]:
        a_test = dict(a_base)
        a_test["placement_confidence"] = conf
        out = OUTPUT_DIR / "after" / f"sv_marked_493_conf{int(conf*100):02d}.png"
        ok = run_markup(source_img, a_test, out, label=f" conf={conf}")


def test_fov_comparison(analysis_data):
    """Test same candidate at FOV 60/90/120 to visualize FOV correction."""
    print(f"\n{'='*60}")
    print("FOV comparison (VP 1059 analysis, varying FOV)")
    print(f"{'='*60}")

    vp = analysis_data.get(1059)
    if not vp:
        print("  ❌ VP 1059 not found")
        return

    a_base = dict(vp.get("analysis", {}))

    # Use the standard image for all FOV tests (to see box size change)
    source_img = IMAGES_DIR / "detail_sv_1059_right.png"
    if not source_img.exists():
        print(f"  ❌ source image not found")
        return

    for fov in [60, 90, 120]:
        a_test = dict(a_base)
        a_test["fov"] = fov
        out = OUTPUT_DIR / "after" / f"sv_marked_1059_fov{fov}.png"
        run_markup(source_img, a_test, out, fov=fov, label=f" fov={fov}")


def main():
    print("Markup Experiments A0+D — Test Runner")
    print(f"Report dir: {REPORT_DIR}")
    print(f"Output dir: {OUTPUT_DIR}")

    setup()
    analysis_data = load_analysis()
    print(f"Loaded {len(analysis_data)} candidates from sv_detail_analysis.json")

    # ── Test 1: VP 493 (recommended, cobblestone, bollards) ──
    # best_image_idx=8 → detail_sv_696_left.png
    test_candidate(
        vp_idx=493,
        analysis_data=analysis_data,
        source_img_name="detail_sv_696_left.png",
        side="left",
    )

    # ── Test 2: VP 784 (doors/windows, shopping alley) ──
    # best_image_idx=5 → detail_sv_494_right.png, confidence=0.45 (low!)
    test_candidate(
        vp_idx=784,
        analysis_data=analysis_data,
        source_img_name="detail_sv_494_right.png",
        side="right",
    )

    # ── Test 3: VP 1059 (open plaza) ──
    # best_image_idx=5 → detail_sv_1059_right.png, also has tight FOV=60
    test_candidate(
        vp_idx=1059,
        analysis_data=analysis_data,
        source_img_name="detail_sv_1059_right.png",
        side="right",
        extra_tests=[
            {"img": "detail_sv_1059_tight_right.png", "fov": 60, "label": "tight_right"},
        ],
    )

    # ── Test 4: Confidence edge cases ──
    test_confidence_edge_cases(analysis_data)

    # ── Test 5: FOV comparison ──
    test_fov_comparison(analysis_data)

    # Summary
    before_count = len(list((OUTPUT_DIR / "before").glob("*.png")))
    after_count = len(list((OUTPUT_DIR / "after").glob("*.png")))
    fov60_count = len(list((OUTPUT_DIR / "after_fov60").glob("*.png")))

    print(f"\n{'='*60}")
    print(f"DONE — {before_count} before, {after_count} after, {fov60_count} fov60 images")
    print(f"Before: {OUTPUT_DIR / 'before'}")
    print(f"After:  {OUTPUT_DIR / 'after'}")
    print(f"FOV60:  {OUTPUT_DIR / 'after_fov60'}")
    print(f"\nOpen in Preview.app to compare:")
    print(f"  open '{OUTPUT_DIR / 'before'}' '{OUTPUT_DIR / 'after'}'")


if __name__ == "__main__":
    main()
