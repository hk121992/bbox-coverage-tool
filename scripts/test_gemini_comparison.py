#!/usr/bin/env python3
"""
test_gemini_comparison.py — Compare Gemini vs Opus for SV placement analysis.

Runs the same grouped detail analysis prompt through Gemini (Flash + Pro)
on existing Liège data and compares outputs against the Opus results.

Usage:
    python3 scripts/test_gemini_comparison.py [--model gemini-2.5-flash]

Requires:
    GEMINI_API_KEY in .env (or environment)
    pip install google-genai
"""

import json
import os
import sys
import time
from pathlib import Path

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "scripts"))

REPORT_DIR = project_root / "data" / "local_reports" / "Liège_0.5km_20260302"
ANALYSIS_PATH = REPORT_DIR / "sv_detail_analysis.json"
OUTPUT_DIR = REPORT_DIR / "gemini_comparison"


def load_env():
    """Load .env file."""
    try:
        from dotenv import load_dotenv
        load_dotenv(project_root / ".env", override=True)
    except ImportError:
        pass


def build_prompt(n_images, image_summary):
    """Build the same grouped analysis prompt used for Opus."""
    return (
        "You are assessing a street location for a bpost bbox parcel locker installation.\n\n"
        "You are viewing MULTIPLE angles of the SAME candidate location — including:\n"
        "- Standard left/right views (perpendicular to street, fov=90°)\n"
        "- Wide context views (fov=120°) for broader scene understanding\n"
        "- Tight detail views (fov=60°) for close-up of wall/frontage\n"
        "- 'Look-toward' views from nearby positions converging on this spot\n"
        "- Junction branch views where streets meet\n\n"
        "Cross-reference all available angles to build a complete understanding of the space.\n"
        "If a view is blocked by a vehicle or obstacle, check other angles to see behind it.\n\n"
        "IMPORTANT — SCENE CLASSIFICATION:\n"
        "Some images may show INTERIOR spaces (inside shops, malls, gyms, covered galleries)\n"
        "or UNSUITABLE scenes (parks with no wall, construction sites, narrow alleys, parking lots).\n"
        "You MUST classify the scene before assessing placement:\n"
        "- scene_type: 'exterior' | 'interior' | 'covered' (covered = arcade, gallery, canopy)\n"
        "- is_viable_exterior: false if scene is fundamentally unsuitable for outdoor locker placement\n"
        "- interior_image_indices: list of 0-based image indices showing indoor/covered scenes\n"
        "If ALL images are interior/covered → set placement_score=0 and is_viable_exterior=false.\n"
        "Interior photos show store aisles, gym equipment, mall galleries, indoor ceilings, etc.\n\n"
        "Available locker sizes:\n"
        "  Compact:  0.6m wide × 0.7m deep × 2.0m tall  (needs 1.2m clear passage)\n"
        "  Standard: 1.2m wide × 0.7m deep × 2.0m tall  (needs 1.5m clear passage)\n"
        "  Large:    2.4m wide × 0.7m deep × 2.0m tall  (needs 1.5m clear passage)\n"
        "  XL:       4.8m+ wide × 0.7m deep × 2.0m tall (needs 2.0m clear passage)\n\n"
        "Requirements for ANY size: level paved ground, unobstructed wall/frontage,\n"
        "no blocking of exits, wheelchair access, or shop entrances.\n\n"
        f"Images show {n_images} views ({image_summary}) of the candidate location.\n\n"
        "Respond ONLY with valid JSON (no markdown fences):\n"
        '{\n'
        '  "scene_type": "exterior"|"interior"|"covered",\n'
        '  "is_viable_exterior": true|false,\n'
        '  "interior_image_indices": [],\n'
        '  "largest_viable_size": "compact"|"standard"|"large"|"xl"|"none",\n'
        '  "placement_score": 0-10,\n'
        '  "placement_confidence": 0.0-1.0,\n'
        '  "confidence": "high"|"medium"|"low",\n'
        '  "best_side": "left"|"right"|"junction"|"none",\n'
        '  "best_image_idx": 0,\n'
        '  "placement_x_pct": 0-100,\n'
        '  "placement_surface": "sidewalk"|"road"|"parking"|"plaza",\n'
        '  "sidewalk_x_range": [20, 45],\n'
        '  "frame_coverage_pct": 60,\n'
        '  "available_width_m": 1.5,\n'
        '  "footpath_width_estimate": "e.g. ~2.5m, adequate for standard",\n'
        '  "obstacles": ["list"],\n'
        '  "blocked_views": ["list of image indices that are blocked/obstructed"],\n'
        '  "wall_available": true|false,\n'
        '  "surface": "paved"|"cobblestone"|"unpaved"|"unclear",\n'
        '  "notes": "2-3 sentences synthesising findings from multiple angles"\n'
        '}\n'
        'scene_type: classify the overall scene — exterior (street), interior (indoors), or covered (arcade/gallery).\n'
        'is_viable_exterior: false if the scene is indoors, covered, or lacks any suitable outdoor wall/frontage.\n'
        'interior_image_indices: 0-based indices of images that show indoor or covered scenes.\n'
        'placement_x_pct: horizontal center of locker as % of best_image_idx width.\n'
        '  CRITICAL: This MUST be on the SIDEWALK/FOOTPATH, never on the road, parking lane, or car space.\n'
        '  Identify where the paved walkway is — it is typically the narrower strip between the building\n'
        '  facade and the road/parking area. Place the locker against the building wall on the footpath.\n'
        '  If best_side="left", the sidewalk is usually in the LEFT portion of the image.\n'
        '  If best_side="right", the sidewalk is usually in the RIGHT portion of the image.\n'
        'placement_surface: what surface the locker would sit on at placement_x_pct — "sidewalk", "road", "parking", or "plaza".\n'
        'sidewalk_x_range: [left_pct, right_pct] — approximate x-range (0-100) where the sidewalk/footpath is visible in the image.\n'
        'placement_confidence: 0.0-1.0 — how confident you are in the exact placement_x_pct location.\n'
        'frame_coverage_pct: what % of image width the available wall/frontage spans (e.g. 40 if wall covers 40% of frame).\n'
        'best_image_idx: 0-based index into the images provided (choose the clearest EXTERIOR view).\n'
        'blocked_views: indices of images that are blocked by vehicles, construction, etc.'
    )


def run_gemini(client, model_name, candidate, max_images=12):
    """Run Gemini analysis on a single candidate."""
    from PIL import Image

    images = candidate.get("images", [])[:max_images]

    # Load images
    pil_images = []
    for img_info in images:
        p = Path(img_info.get("path", ""))
        if p.exists():
            pil_images.append(Image.open(p))

    if not pil_images:
        return None

    # Build image summary
    by_type = {}
    for img in images:
        t = img.get("capture_type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1
    summary = ", ".join(f"{v} {k}" for k, v in by_type.items())

    prompt = build_prompt(len(pil_images), summary)

    # Send to Gemini
    contents = [prompt] + pil_images

    try:
        response = client.models.generate_content(
            model=model_name,
            contents=contents,
        )
        text = response.text.strip()
        # Try to parse JSON from response
        # Remove markdown fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3].strip()
        if text.startswith("json"):
            text = text[4:].strip()

        result = json.loads(text)
        return result
    except json.JSONDecodeError as e:
        return {"_error": f"JSON parse error: {e}", "_raw": response.text[:500]}
    except Exception as e:
        return {"_error": str(e)}


def compare_results(opus, gemini, vp_idx):
    """Compare Opus vs Gemini results for one candidate."""
    fields = [
        "placement_score", "largest_viable_size", "placement_x_pct",
        "placement_confidence", "best_side", "frame_coverage_pct",
        "available_width_m", "scene_type", "wall_available",
        "placement_surface",
    ]

    row = {"vp_idx": vp_idx}
    for f in fields:
        opus_val = opus.get(f, "—")
        gemini_val = gemini.get(f, "—")
        row[f"opus_{f}"] = opus_val
        row[f"gemini_{f}"] = gemini_val

        # Mark agreement
        if opus_val == gemini_val:
            row[f"match_{f}"] = True
        elif isinstance(opus_val, (int, float)) and isinstance(gemini_val, (int, float)):
            row[f"match_{f}"] = abs(opus_val - gemini_val) <= 1  # within 1 point
        else:
            row[f"match_{f}"] = False

    row["opus_notes"] = opus.get("notes", "")[:80]
    row["gemini_notes"] = gemini.get("notes", "")[:80]
    row["gemini_sidewalk_x_range"] = gemini.get("sidewalk_x_range", "—")

    return row


def print_comparison_table(comparisons, model_name):
    """Print a formatted comparison table."""
    print(f"\n{'=' * 120}")
    print(f"COMPARISON: Opus vs {model_name}")
    print(f"{'=' * 120}")

    print(f"\n{'VP':>5} | {'Score':^11} | {'Size':^23} | {'X_pct':^13} | {'Conf':^11} | {'Side':^13} | {'Surface':^20}")
    print(f"{'':>5} | {'Opus Gem':^11} | {'Opus':^11} {'Gem':^11} | {'Opus Gem':^13} | {'Opus Gem':^11} | {'Opus':^6} {'Gem':^6} | {'Gem':^20}")
    print("-" * 120)

    score_diffs = []
    x_diffs = []
    size_matches = 0
    side_matches = 0

    for c in comparisons:
        vp = c["vp_idx"]
        o_score = c.get("opus_placement_score", "—")
        g_score = c.get("gemini_placement_score", "—")
        o_size = c.get("opus_largest_viable_size", "—")
        g_size = c.get("gemini_largest_viable_size", "—")
        o_x = c.get("opus_placement_x_pct", "—")
        g_x = c.get("gemini_placement_x_pct", "—")
        o_conf = c.get("opus_placement_confidence", "—")
        g_conf = c.get("gemini_placement_confidence", "—")
        o_side = c.get("opus_best_side", "—")
        g_side = c.get("gemini_best_side", "—")
        g_surface = c.get("gemini_placement_surface", "—")

        score_match = "✓" if c.get("match_placement_score") else "✗"
        size_match = "✓" if c.get("match_largest_viable_size") else "✗"
        side_match = "✓" if c.get("match_best_side") else "✗"

        if isinstance(o_score, (int, float)) and isinstance(g_score, (int, float)):
            score_diffs.append(abs(o_score - g_score))
        if isinstance(o_x, (int, float)) and isinstance(g_x, (int, float)):
            x_diffs.append(abs(o_x - g_x))
        if o_size == g_size:
            size_matches += 1
        if o_side == g_side:
            side_matches += 1

        print(f"{vp:>5} | {o_score:>3} {g_score:>3} {score_match:>2} | {str(o_size):^11} {str(g_size):^11} | {o_x:>4} {g_x:>4}    | {o_conf:>4} {g_conf:>4}  | {str(o_side):^6} {str(g_side):^6} | {str(g_surface):^20}")

    n = len(comparisons)
    print("-" * 120)
    print(f"\nSummary ({n} candidates):")
    if score_diffs:
        print(f"  Score: avg diff = {sum(score_diffs)/len(score_diffs):.1f}, max diff = {max(score_diffs)}")
    if x_diffs:
        print(f"  X_pct: avg diff = {sum(x_diffs)/len(x_diffs):.0f}%, max diff = {max(x_diffs)}%")
    print(f"  Size match: {size_matches}/{n} ({size_matches/n*100:.0f}%)")
    print(f"  Side match: {side_matches}/{n} ({side_matches/n*100:.0f}%)")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gemini-2.5-flash",
                        help="Gemini model to use")
    parser.add_argument("--max-candidates", type=int, default=17,
                        help="Max candidates to test")
    parser.add_argument("--max-images", type=int, default=8,
                        help="Max images per candidate (to control cost)")
    args = parser.parse_args()

    load_env()
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY not set in .env")
        sys.exit(1)

    from google import genai
    client = genai.Client(api_key=api_key)
    print(f"Gemini model: {args.model}")
    print(f"Max candidates: {args.max_candidates}")
    print(f"Max images per candidate: {args.max_images}")

    # Load existing Opus analysis
    with open(ANALYSIS_PATH) as f:
        data = json.load(f)

    candidates = data.get("top_candidates", [])[:args.max_candidates]
    print(f"Loaded {len(candidates)} candidates from {ANALYSIS_PATH.name}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    comparisons = []
    gemini_results = []

    for i, cand in enumerate(candidates):
        vp_idx = cand.get("viewpoint_idx", 0)
        opus_result = cand.get("analysis", {})
        n_imgs = len(cand.get("images", []))

        print(f"\n[{i+1}/{len(candidates)}] VP {vp_idx} ({n_imgs} images)...")

        result = run_gemini(client, args.model, cand, max_images=args.max_images)

        if result is None:
            print(f"  SKIPPED — no images")
            continue

        if "_error" in result:
            print(f"  ERROR: {result['_error']}")
            gemini_results.append({"vp_idx": vp_idx, "error": result["_error"]})
            continue

        # Compare
        comp = compare_results(opus_result, result, vp_idx)
        comparisons.append(comp)
        gemini_results.append({"vp_idx": vp_idx, **result})

        o_score = opus_result.get("placement_score", "—")
        g_score = result.get("placement_score", "—")
        o_x = opus_result.get("placement_x_pct", "—")
        g_x = result.get("placement_x_pct", "—")
        g_surface = result.get("placement_surface", "—")
        print(f"  Opus: score={o_score}, x={o_x}%")
        print(f"  Gemini: score={g_score}, x={g_x}%, surface={g_surface}")

        # Rate limit: small delay between calls
        time.sleep(1.0)

    # Print comparison table
    print_comparison_table(comparisons, args.model)

    # Save results
    out_file = OUTPUT_DIR / f"gemini_{args.model.replace('.', '_')}_results.json"
    with open(out_file, "w") as f:
        json.dump({
            "model": args.model,
            "n_candidates": len(candidates),
            "max_images": args.max_images,
            "comparisons": comparisons,
            "gemini_raw": gemini_results,
        }, f, indent=2, default=str)
    print(f"\nResults saved to {out_file}")


if __name__ == "__main__":
    main()
