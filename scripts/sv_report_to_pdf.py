#!/usr/bin/env python3
"""
sv_report_to_pdf.py — v2.0 Professional PDF for SV Corridor Analysis.

Generates an actionable report modelled on report_to_pdf.py.  Two modes:
  Mode A: Viable candidates found → full report with enriched detail pages
  Mode B: No viable placement    → explanation + screening appendix

Usage:
  python3 scripts/sv_report_to_pdf.py <report_dir>
  python3 scripts/sv_report_to_pdf.py data/local_reports/Ixelles---Elsene_0.5km_20260228
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from fpdf import FPDF


def sanitize(text):
    """Replace problematic Unicode chars with ASCII equivalents."""
    if not text:
        return ""
    result = (str(text)
              .replace("\u2014", "-")
              .replace("\u2013", "-")
              .replace("\u2018", "'")
              .replace("\u2019", "'")
              .replace("\u201c", '"')
              .replace("\u201d", '"')
              .replace("\u2026", "...")
              .replace("\u00d7", "x")
              .replace("\u2264", "<=")
              .replace("\u2265", ">=")
              .replace("\u2248", "~")
              .replace("\u2022", "-")
              .replace("\u00b2", "2")
              .replace("\u00b0", "deg")
              .replace("\u00e9", "e")
              .replace("\u00e8", "e")
              .replace("\u00e0", "a")
              .replace("\u00e7", "c")
              .replace("\u00f4", "o")
              .replace("\u00fb", "u")
              .replace("\u00e2", "a")
              .replace("\u00ee", "i")
              .replace("\u00eb", "e")
              .replace("\u00fc", "u")
              .replace("\u00f6", "o")
              .replace("\u00e4", "a")
              .replace("\u00ef", "i")
              .replace("\u0027", "'"))
    # Strip any remaining non-latin1 characters
    return result.encode("latin-1", errors="replace").decode("latin-1")


SIZE_COLORS = {
    "compact":  {"fill": (254, 249, 195), "text": (161, 98, 7)},
    "standard": {"fill": (220, 252, 231), "text": (22, 163, 74)},
    "large":    {"fill": (219, 234, 254), "text": (37, 99, 235)},
    "xl":       {"fill": (243, 232, 255), "text": (126, 34, 206)},
}


class SVReportPDF(FPDF):
    """PDF class with report helper methods."""

    def header(self):
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(100, 100, 100)
        self.cell(0, 6, sanitize("SV Corridor Analysis v2.0 - bpost Locker Placement"),
                  align="R")
        self.ln(8)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 10, f"Page {self.page_no()}/{{nb}}", align="C")

    def section_title(self, title):
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(30, 58, 138)
        self.cell(0, 10, sanitize(title))
        self.ln(8)
        self.set_draw_color(59, 130, 246)
        self.set_line_width(0.5)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(4)

    def sub_title(self, title):
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(55, 65, 81)
        self.cell(0, 8, sanitize(title))
        self.ln(6)

    def kv(self, key, value, bold_value=False):
        self.set_font("Helvetica", "", 9)
        self.set_text_color(100, 100, 100)
        self.cell(48, 5, sanitize(key + ":"))
        self.set_text_color(30, 30, 30)
        self.set_font("Helvetica", "B" if bold_value else "", 9)
        self.cell(0, 5, sanitize(str(value)))
        self.ln(5)

    def body_text(self, text, size=9):
        self.set_font("Helvetica", "", size)
        self.set_text_color(55, 65, 81)
        self.multi_cell(0, 4.5, sanitize(text))
        self.ln(2)

    def score_bar(self, score, max_score=10, width=30):
        x, y = self.get_x(), self.get_y()
        self.set_fill_color(229, 231, 235)
        self.rect(x, y, width, 4, "F")
        pct = min(score / max(max_score, 1), 1.0)
        if pct >= 0.7:
            self.set_fill_color(34, 197, 94)
        elif pct >= 0.4:
            self.set_fill_color(234, 179, 8)
        else:
            self.set_fill_color(239, 68, 68)
        if pct > 0:
            self.rect(x, y, width * pct, 4, "F")
        self.set_x(x + width + 2)
        self.set_font("Helvetica", "B", 8)
        self.set_text_color(30, 30, 30)
        self.cell(14, 4, f"{score}/{max_score}")

    def size_badge(self, size_name):
        colors = SIZE_COLORS.get(size_name, {"fill": (229, 231, 235), "text": (100, 100, 100)})
        self.set_fill_color(*colors["fill"])
        self.set_text_color(*colors["text"])
        self.set_font("Helvetica", "B", 9)
        self.cell(28, 6, sanitize(size_name.upper()), border=0, fill=True, align="C")
        self.set_text_color(30, 30, 30)

    def verdict_badge(self, verdict):
        v = verdict.lower()
        if "feasible" in v and "not" not in v:
            self.set_fill_color(220, 252, 231)
            color = (22, 163, 74)
        elif "marginal" in v:
            self.set_fill_color(254, 249, 195)
            color = (161, 98, 7)
        else:
            self.set_fill_color(254, 226, 226)
            color = (220, 38, 38)
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(*color)
        self.cell(30, 6, sanitize(verdict), border=0, fill=True, align="C")
        self.set_text_color(30, 30, 30)

    def safe_image(self, path, x=None, y=None, w=0, h=0):
        """Add image to PDF, returning True on success."""
        p = Path(path)
        if not p.exists():
            return False
        try:
            self.image(str(p), x=x, y=y, w=w, h=h)
            return True
        except Exception as e:
            print(f"  PDF image error ({p.name}): {e}")
            return False


def generate_sv_report_pdf(report_dir):
    """Generate a professional PDF from SV corridor analysis output.

    Reads all JSON outputs from report_dir and produces sv_report.pdf.
    """
    report_dir = Path(report_dir)
    img_dir = report_dir / "images"

    # Load all available data files
    sv_report_path = report_dir / "sv_report.json"
    screen_path = report_dir / "sv_screen_analysis.json"
    detail_path = report_dir / "sv_detail_analysis.json"
    enrichment_path = report_dir / "sv_enrichment.json"
    report_json_path = report_dir / "report.json"

    sv_report = {}
    screen_data = {}
    detail_data = {}
    enrichment = {}
    main_report = {}

    if sv_report_path.exists():
        with open(sv_report_path) as f:
            sv_report = json.load(f)
    if screen_path.exists():
        with open(screen_path) as f:
            screen_data = json.load(f)
    if detail_path.exists():
        with open(detail_path) as f:
            detail_data = json.load(f)
    if enrichment_path.exists():
        with open(enrichment_path) as f:
            enrichment = json.load(f)
    if report_json_path.exists():
        with open(report_json_path) as f:
            main_report = json.load(f)

    # Determine candidates and mode
    candidates = sv_report.get("top_candidates", [])
    enriched_candidates = enrichment.get("candidates", [])
    recommendation = enrichment.get("recommendation", {})
    zoning_analysis = enrichment.get("zoning_analysis", {})

    # Check for viable candidates
    viable = [c for c in enriched_candidates
              if c.get("physical_feasibility", {}).get("verdict", "").lower()
              in ("feasible", "marginal")]
    if not viable and candidates:
        # Fall back to SV analysis candidates
        viable = [c for c in candidates
                  if c.get("placement_score", 0) >= 5]

    has_viable = len(viable) > 0
    # Resolve sector from all available sources
    sector = "unknown"
    for src in [sv_report, detail_data, screen_data]:
        s = src.get("meta", {}).get("sector", "unknown")
        if s and s != "unknown":
            sector = s
            break
    if sector == "unknown":
        # Try main report
        sector = main_report.get("meta", {}).get("target_sector", "unknown")
    if sector == "unknown":
        # Try directory name (e.g. Ixelles---Elsene_0.5km_20260228)
        dir_name = report_dir.name
        sector = dir_name.split("_")[0] if dir_name else "unknown"

    # Try to find the numeric sector code (for corridor map filenames)
    sector_code_hint = ""
    # Check sv_path_*.json files in data/
    abs_report = report_dir.resolve()
    data_dir = abs_report.parent.parent if abs_report.parent.name == "local_reports" else abs_report.parent
    for sv_path_file in data_dir.glob("sv_path_*.json"):
        code = sv_path_file.stem.replace("sv_path_", "")
        if code:
            sector_code_hint = code
            break
    # Also check enrichment/screen meta for sector_code field
    for src in [enrichment, screen_data, detail_data, sv_report]:
        sc = src.get("meta", {}).get("sector_code", "")
        if sc:
            sector_code_hint = sc
            break

    commune = enrichment.get("commune",
              main_report.get("meta", {}).get("area_name", ""))
    # Clean commune from directory-style names (e.g. "Ixelles---Elsene_0.5km" → "Ixelles - Elsene")
    if not commune or commune == sector:
        # Extract from directory name — take first part before _radius or _date
        dir_parts = report_dir.name.split("_")
        if dir_parts:
            commune = dir_parts[0].replace("---", " - ")
    if commune:
        commune = commune.replace("---", " - ")
        # Strip trailing radius/date suffixes like "_0.5km" that may have leaked in
        import re
        commune = re.sub(r'[_\s]*\d+(\.\d+)?km$', '', commune).strip()

    # PDF setup
    pdf = SVReportPDF(orientation="P", unit="mm", format="A4")
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=18)

    # ─── PAGE 1: TITLE ────────────────────────────────────────────
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 22)
    pdf.set_text_color(30, 58, 138)
    pdf.cell(0, 14, sanitize("SV Corridor Analysis Report"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 12)
    pdf.set_text_color(100, 100, 100)
    # Show sector and commune (prefer numeric sector code, clean display)
    display_sector = sector_code_hint if sector_code_hint else sector
    if commune and commune != display_sector and commune != "unknown":
        subtitle = f"Sector: {display_sector}  |  {commune}"
    else:
        subtitle = f"Sector: {display_sector}"
    pdf.cell(0, 8, sanitize(subtitle), new_x="LMARGIN", new_y="NEXT")
    ts = sv_report.get("meta", {}).get("generated",
         detail_data.get("meta", {}).get("at", datetime.utcnow().isoformat()))
    pdf.cell(0, 6, sanitize(f"Generated: {ts[:19]}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Overview satellite image
    overview = img_dir / "overview_satellite.png"
    if pdf.safe_image(overview, w=170):
        pdf.ln(4)

    # Pipeline summary
    pdf.sub_title("Pipeline Summary")
    n_screen = screen_data.get("meta", {}).get("n_analysed", 0)
    n_detail = detail_data.get("meta", {}).get("n_analysed",
               detail_data.get("meta", {}).get("n_groups", 0))
    n_viable_screen = screen_data.get("meta", {}).get("n_viable", 0)
    n_viable_detail = detail_data.get("meta", {}).get("n_viable", 0)
    n_enriched = len(enriched_candidates)
    n_final_viable = len(viable)

    pdf.kv("Pass 1 (Screening)", f"{n_screen} viewpoints, {n_viable_screen} viable")
    pdf.kv("Pass 2 (Detail)", f"{n_detail} groups, {n_viable_detail} viable")
    if n_enriched:
        pdf.kv("Enriched candidates", str(n_enriched))
    pdf.kv("Final viable", f"{n_final_viable} candidates",
           bold_value=n_final_viable > 0)
    pdf.kv("Analysis version", "2.0")
    pdf.ln(4)

    # ─── PAGE 2: CORRIDOR MAP ─────────────────────────────────────
    # Look for corridor map images — try both display sector name and numeric sector code
    corridor_map = None
    # Resolve to absolute paths for reliable traversal
    abs_report = report_dir.resolve()
    project_root = abs_report.parent.parent  # .../data/local_reports/X → .../data
    if project_root.name in ("local_reports", "data"):
        # Walk up until we're at the actual project root (has scripts/ dir)
        while project_root.parent != project_root:
            if (project_root / "scripts").is_dir():
                break
            project_root = project_root.parent
    # Also try the numeric sector code from analysis meta or sv_path hint
    sector_code = sector_code_hint  # already resolved above
    if not sector_code:
        for src in [screen_data, detail_data, sv_report]:
            sc = src.get("meta", {}).get("sector", "")
            if sc and sc != "unknown" and any(ch.isdigit() for ch in sc):
                sector_code = sc
                break
    if not sector_code:
        sector_code = main_report.get("meta", {}).get("target_sector", "")

    search_names = set()
    for s in [sector, sector_code]:
        if s and s != "unknown":
            search_names.add(f"sector_{s}_corridor_markup.png")
            search_names.add(f"sector_{s}_sv_corridor.png")
    search_names.add("corridor_map.png")

    search_dirs = [project_root, report_dir, img_dir]
    for pattern in search_names:
        for d in search_dirs:
            candidate_path = d / pattern
            if candidate_path.exists():
                corridor_map = candidate_path
                break
        if corridor_map:
            break
    # Also glob project root for any corridor markup png
    if not corridor_map:
        for p in sorted(project_root.glob("sector_*_corridor_markup.png")):
            corridor_map = p
            break

    if corridor_map:
        pdf.add_page()
        pdf.section_title("Corridor Map")
        pdf.body_text(
            "The ML-guided corridor shows the streets surveyed by the Street View analysis. "
            "Hot edges (red/orange) indicate high placement potential from the ML model. "
            "Blue dots mark viewpoint positions where Street View imagery was captured. "
            "Numbered pins show the locations of the top placement candidates."
        )
        pdf.safe_image(corridor_map, w=170)
        pdf.ln(4)

    # ─── PAGE 3: EXECUTIVE SUMMARY / NO-VIABLE ────────────────────
    pdf.add_page()

    if has_viable:
        pdf.section_title("Executive Summary")

        # Recommendation
        if recommendation:
            winner_id = recommendation.get("winner_id")
            reasoning = recommendation.get("reasoning", "")
            next_steps = recommendation.get("next_steps", [])

            if winner_id:
                pdf.sub_title(f"Recommendation: Candidate #{winner_id}")
            else:
                pdf.sub_title("Recommendation")

            if reasoning:
                pdf.body_text(reasoning)

            if next_steps:
                pdf.sub_title("Next Steps")
                for i, step in enumerate(next_steps, 1):
                    pdf.set_font("Helvetica", "", 9)
                    pdf.set_text_color(55, 65, 81)
                    pdf.cell(8, 5, f"{i}.")
                    pdf.multi_cell(0, 5, sanitize(step))
                    pdf.ln(1)
            pdf.ln(4)

        # Ranked candidate table
        pdf.sub_title("Candidate Rankings")
        # Table header
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_fill_color(243, 244, 246)
        pdf.set_text_color(55, 65, 81)
        pdf.cell(10, 6, "#", border=1, fill=True, align="C")
        pdf.cell(55, 6, "Address", border=1, fill=True)
        pdf.cell(15, 6, "Score", border=1, fill=True, align="C")
        pdf.cell(22, 6, "Size", border=1, fill=True, align="C")
        pdf.cell(25, 6, "Verdict", border=1, fill=True, align="C")
        pdf.cell(15, 6, "IDW", border=1, fill=True, align="C")
        pdf.cell(35, 6, "Key Factor", border=1, fill=True)
        pdf.ln()

        display_candidates = enriched_candidates if enriched_candidates else candidates
        for c in display_candidates:
            cid = c.get("id", c.get("rank", "?"))
            addr = c.get("address", "")[:35] or f"VP {c.get('sv_viewpoint_idx', c.get('viewpoint_idx', ''))}"
            score = c.get("sv_placement_score", c.get("placement_score", 0))
            size = c.get("sv_largest_viable_size", c.get("largest_viable_size", ""))
            verdict = c.get("physical_feasibility", {}).get("verdict", "")
            idw = c.get("ml_score", c.get("idw_score", 0))
            notes = c.get("sv_notes", c.get("notes", ""))[:22]

            pdf.set_font("Helvetica", "", 8)
            pdf.set_text_color(30, 30, 30)
            pdf.cell(10, 6, str(cid), border=1, align="C")
            pdf.cell(55, 6, sanitize(addr), border=1)
            pdf.cell(15, 6, f"{score}/10", border=1, align="C")
            # Size with color
            sc = SIZE_COLORS.get(size, {"fill": (255, 255, 255), "text": (0, 0, 0)})
            pdf.set_fill_color(*sc["fill"])
            pdf.set_text_color(*sc["text"])
            pdf.cell(22, 6, sanitize(size.upper() if size else "-"), border=1, fill=True, align="C")
            pdf.set_text_color(30, 30, 30)
            # Verdict
            if verdict:
                v = verdict.lower()
                if "feasible" in v and "not" not in v:
                    pdf.set_text_color(22, 163, 74)
                elif "marginal" in v:
                    pdf.set_text_color(161, 98, 7)
                else:
                    pdf.set_text_color(220, 38, 38)
                pdf.cell(25, 6, sanitize(verdict), border=1, align="C")
                pdf.set_text_color(30, 30, 30)
            else:
                pdf.cell(25, 6, "-", border=1, align="C")
            pdf.cell(15, 6, f"{idw:.2f}" if isinstance(idw, float) else str(idw), border=1, align="C")
            pdf.cell(35, 6, sanitize(notes), border=1)
            pdf.ln()

    else:
        # Mode B: No viable placement
        pdf.section_title("No Viable Placement Found")
        pdf.body_text(
            "After analysing the ML corridor through this sector using multi-angle "
            "Street View imagery, no location was found that meets the minimum "
            "requirements for bpost bbox locker installation."
        )
        pdf.ln(2)

        # Explain why
        pdf.sub_title("Why No Placement Was Found")
        # Collect common obstacles from screening
        all_obstacles = []
        for vp in screen_data.get("viewpoints", []):
            obs = vp.get("analysis", {}).get("obstacles", [])
            if isinstance(obs, list):
                all_obstacles.extend(obs)
        if all_obstacles:
            from collections import Counter
            common = Counter(all_obstacles).most_common(8)
            pdf.body_text("Most common obstacles observed in the surveyed corridor:")
            for obs, count in common:
                pdf.set_font("Helvetica", "", 9)
                pdf.cell(8, 5, "")
                pdf.cell(0, 5, sanitize(f"- {obs} (seen {count}x)"))
                pdf.ln(4)
        pdf.ln(2)

        # Alternatives
        pdf.sub_title("Suggested Alternatives")
        pdf.body_text(
            "- Widen the search radius to include adjacent sectors\n"
            "- Consider non-street placements (building lobbies, parking structures)\n"
            "- Survey neighbouring commercial zones with wider footpaths\n"
            "- Re-assess after planned street renovations (check commune plans)"
        )

    # ─── CANDIDATE DETAIL PAGES ───────────────────────────────────
    detail_list = enriched_candidates if enriched_candidates else []
    for c in detail_list:
        verdict = c.get("physical_feasibility", {}).get("verdict", "")
        # Only show detail pages for Feasible/Marginal
        if verdict.lower() not in ("feasible", "marginal") and verdict:
            continue

        pdf.add_page()
        cid = c.get("id", "?")
        vidx = c.get("sv_viewpoint_idx", c.get("viewpoint_idx", 0))

        # Header with score + size + verdict
        pdf.set_font("Helvetica", "B", 13)
        pdf.set_text_color(30, 58, 138)
        pdf.cell(0, 10, sanitize(f"Candidate #{cid}"), new_x="LMARGIN", new_y="NEXT")

        # Badge row
        x_start = pdf.get_x()
        y_start = pdf.get_y()
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(15, 6, "Score: ")
        score = c.get("sv_placement_score", c.get("placement_score", 0))
        pdf.score_bar(score, 10, 30)
        pdf.cell(5, 4, "")
        size = c.get("sv_largest_viable_size", c.get("largest_viable_size", ""))
        if size and size != "none":
            pdf.size_badge(size)
        pdf.cell(5, 6, "")
        if verdict:
            pdf.verdict_badge(verdict)
        pdf.ln(8)

        # Address + zoning
        addr = c.get("address", "")
        if addr:
            pdf.kv("Address", addr, bold_value=True)
        pdf.kv("Coordinates", f"{c.get('lat', 0):.5f}, {c.get('lng', 0):.5f}")
        zd = c.get("zoning_data", {})
        if zd:
            zone = zd.get("zone_name_fr") or zd.get("zone_name_nl") or zd.get("zone_type", "")
            if zone:
                pdf.kv("Zoning", zone)
        suggested = c.get("suggested_site", {})
        if suggested:
            pdf.kv("Nearest POI", f"{suggested.get('name', '')} ({suggested.get('type', '')},"
                   f" {suggested.get('dist_m', '?')}m)")
        pdf.kv("Nearest locker", f"{c.get('nearest_existing_m', '?')}m")
        pdf.kv("Competitors 500m", str(c.get("competitors_nearby", 0)))
        pdf.ln(2)

        # Commentary
        commentary = c.get("commentary", "")
        if commentary:
            pdf.body_text(commentary)
            pdf.ln(1)

        # Annotated SV image (marked up)
        best_side = c.get("sv_best_side", "left")
        marked_path = img_dir / f"sv_marked_{vidx}_{best_side}.png"
        if not marked_path.exists():
            # Try other sides (including wide/tight variants from v2.0)
            for side in ["left", "right", f"wide_{best_side}", f"tight_{best_side}",
                         "wide_left", "wide_right", "tight_left", "tight_right"]:
                alt = img_dir / f"sv_marked_{vidx}_{side}.png"
                if alt.exists():
                    marked_path = alt
                    break
        if not marked_path.exists():
            # Final fallback: glob for any sv_marked_{vidx}_*.png
            matches = sorted(img_dir.glob(f"sv_marked_{vidx}_*.png"))
            if matches:
                marked_path = matches[0]
        if marked_path.exists():
            pdf.safe_image(marked_path, w=170)
            pdf.set_font("Helvetica", "I", 7)
            pdf.set_text_color(120, 120, 120)
            pdf.cell(0, 4, sanitize(f"Annotated view — VP{vidx}, locker overlay "
                     f"({size} {c.get('sv_available_width_m', '?')}m available)"),
                     new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)
        else:
            skip_reason = c.get("markup_skipped_reason", "")
            if skip_reason:
                pdf.set_font("Helvetica", "I", 8)
                pdf.set_text_color(180, 80, 80)
                pdf.cell(0, 5, sanitize(f"Locker overlay not rendered: {skip_reason}"),
                         new_x="LMARGIN", new_y="NEXT")
                pdf.ln(2)

        # SV Gallery (2x2 grid showing different angles)
        sv_images = c.get("sv_images", [])
        if sv_images:
            # Sort images: standard L/R first, then wide, then tight, then others
            def _gallery_priority(img_info):
                if not isinstance(img_info, dict):
                    return 5
                ct = img_info.get("capture_type", "standard")
                side = img_info.get("side", "")
                if ct == "standard" and "left" in side: return 0
                if ct == "standard" and "right" in side: return 1
                if ct == "wide": return 2
                if ct == "tight": return 3
                return 4
            sorted_imgs = sorted(sv_images, key=_gallery_priority)

            # Check space — start new page if needed
            if pdf.get_y() > 180:
                pdf.add_page()
            pdf.sub_title("Street View Gallery")
            col = 0
            shown = 0
            for img_info in sorted_imgs:
                if shown >= 4:
                    break
                p = Path(img_info["path"]) if isinstance(img_info, dict) else Path(img_info)
                if not p.exists():
                    continue
                x = pdf.l_margin + col * 90
                y = pdf.get_y()
                pdf.safe_image(p, x=x, y=y, w=85, h=55)
                # Caption under image
                cap = ""
                if isinstance(img_info, dict):
                    cap = f"{img_info.get('side', '')} (fov={img_info.get('fov', 90)})"
                if cap:
                    pdf.set_font("Helvetica", "I", 6)
                    pdf.set_text_color(120, 120, 120)
                    pdf.set_xy(x, y + 55)
                    pdf.cell(85, 3, sanitize(cap), align="C")
                col += 1
                shown += 1
                if col >= 2:
                    col = 0
                    pdf.set_y(y + 60)
            if col > 0:
                pdf.ln(60)
            else:
                pdf.ln(3)

        # Satellite view
        sat = img_dir / f"candidate_{cid}_satellite.png"
        if sat.exists() and pdf.get_y() < 210:
            pdf.sub_title("Satellite View")
            pdf.safe_image(sat, w=120, h=75)
            pdf.ln(2)

        # Physical Feasibility
        pf = c.get("physical_feasibility", {})
        if pf:
            if pdf.get_y() > 240:
                pdf.add_page()
            pdf.sub_title("Physical Feasibility Assessment")
            for field in ["footpath_assessment", "space_assessment",
                          "accessibility", "visibility_traffic", "obstacles"]:
                val = pf.get(field, "")
                if val:
                    label = field.replace("_", " ").title()
                    pdf.kv(label, val)
            pdf.ln(2)

        # AI Observations
        obs = c.get("visual_observations", [])
        if obs:
            if pdf.get_y() > 250:
                pdf.add_page()
            pdf.sub_title("AI Observations")
            for i, o in enumerate(obs, 1):
                pdf.set_font("Helvetica", "B", 9)
                pdf.set_text_color(59, 130, 246)
                pdf.cell(6, 5, f"{i}.")
                pdf.set_font("Helvetica", "B", 9)
                pdf.set_text_color(30, 30, 30)
                pdf.cell(40, 5, sanitize(o.get("label", "")))
                pdf.set_font("Helvetica", "", 9)
                pdf.set_text_color(55, 65, 81)
                pdf.multi_cell(0, 5, sanitize(o.get("description", "")))
                pdf.ln(1)
            pdf.ln(2)

        # Location Context
        lc = c.get("location_context", {})
        if lc:
            if pdf.get_y() > 250:
                pdf.add_page()
            pdf.sub_title("Location Context")
            transit = lc.get("transit", {})
            if transit:
                pdf.kv("Bus stops (300m)", str(transit.get("bus_stops_300m", 0)))
                pdf.kv("Rail/tram (500m)", str(transit.get("rail_tram_500m", 0)))
                details = transit.get("details", [])
                for d in details[:3]:
                    pdf.set_font("Helvetica", "", 8)
                    pdf.set_text_color(100, 100, 100)
                    pdf.cell(48, 4, "")
                    pdf.cell(0, 4, sanitize(f"  {d.get('name', '')} ({d.get('type', '')}, "
                             f"{d.get('dist_m', '?')}m)"))
                    pdf.ln(4)
            commerce = lc.get("commerce", {})
            if commerce:
                pdf.kv("Shops (300m)", str(commerce.get("shops_300m", 0)))
            parking = lc.get("parking", {})
            if parking:
                pdf.kv("Parking (200m)", str(parking.get("spots_200m", 0)))
            ped = lc.get("pedestrian", {})
            if ped:
                pdf.kv("Footways (150m)", str(ped.get("footways_150m", 0)))
            pdf.ln(2)

        # Contact Details
        cd = c.get("contact_details", {})
        if cd and any(cd.values()):
            if pdf.get_y() > 260:
                pdf.add_page()
            pdf.sub_title("Contact Details")
            for field in ["site_type", "business_name", "parent_company",
                          "contact_approach", "phone_website", "commune_authority"]:
                val = cd.get(field, "")
                if val:
                    label = field.replace("_", " ").title()
                    pdf.kv(label, val)
            pdf.ln(2)

        # Nearby POIs
        pois = c.get("nearby_pois", [])
        if pois:
            if pdf.get_y() > 255:
                pdf.add_page()
            pdf.sub_title("Nearby Points of Interest")
            for poi in pois[:5]:
                pdf.set_font("Helvetica", "", 8)
                pdf.set_text_color(55, 65, 81)
                pdf.cell(8, 4, "")
                pdf.cell(0, 4, sanitize(
                    f"{poi.get('name', '?')} ({poi.get('type', '')}, {poi.get('dist_m', '?')}m)"))
                pdf.ln(4)
            pdf.ln(2)

    # ─── SCREENING APPENDIX ───────────────────────────────────────
    if screen_data.get("viewpoints"):
        pdf.add_page()
        pdf.section_title("Appendix: Screening Results")
        pdf.body_text(
            f"Pass 1 screened {n_screen} viewpoints along the ML corridor "
            f"using Claude Sonnet. {n_viable_screen} showed potential viable "
            f"placement locations."
        )
        pdf.ln(2)

        # Score distribution
        scores = [vp.get("analysis", {}).get("placement_score", 0)
                  for vp in screen_data.get("viewpoints", [])]
        if scores:
            pdf.sub_title("Score Distribution")
            buckets = [0] * 11
            for s in scores:
                buckets[min(s, 10)] += 1
            max_count = max(buckets) or 1
            bar_max_w = 80
            for i in range(11):
                pdf.set_font("Helvetica", "", 7)
                pdf.set_text_color(100, 100, 100)
                pdf.cell(10, 4, f"{i}/10")
                w = (buckets[i] / max_count) * bar_max_w
                if buckets[i] > 0:
                    if i >= 7:
                        pdf.set_fill_color(34, 197, 94)
                    elif i >= 4:
                        pdf.set_fill_color(234, 179, 8)
                    else:
                        pdf.set_fill_color(239, 68, 68)
                    pdf.rect(pdf.get_x(), pdf.get_y(), max(w, 1), 3.5, "F")
                pdf.set_x(pdf.get_x() + bar_max_w + 4)
                pdf.set_text_color(30, 30, 30)
                pdf.cell(10, 4, str(buckets[i]))
                pdf.ln(4)
            pdf.ln(4)

        # Full screening table
        pdf.sub_title("All Screened Viewpoints")
        pdf.set_font("Helvetica", "B", 7)
        pdf.set_fill_color(243, 244, 246)
        pdf.set_text_color(55, 65, 81)
        pdf.cell(10, 5, "VP", border=1, fill=True, align="C")
        pdf.cell(15, 5, "IDW", border=1, fill=True, align="C")
        pdf.cell(12, 5, "Score", border=1, fill=True, align="C")
        pdf.cell(20, 5, "Size", border=1, fill=True, align="C")
        pdf.cell(18, 5, "Surface", border=1, fill=True, align="C")
        pdf.cell(10, 5, "Wall", border=1, fill=True, align="C")
        pdf.cell(92, 5, "Notes", border=1, fill=True)
        pdf.ln()

        for vp in screen_data.get("viewpoints", []):
            a = vp.get("analysis", {})
            vidx = vp.get("viewpoint_idx", 0)
            pdf.set_font("Helvetica", "", 7)
            pdf.set_text_color(30, 30, 30)
            pdf.cell(10, 5, str(vidx), border=1, align="C")
            pdf.cell(15, 5, f"{vp.get('idw_score', 0):.3f}", border=1, align="C")
            pdf.cell(12, 5, f"{a.get('placement_score', 0)}/10", border=1, align="C")
            size = a.get("largest_viable_size", "none")
            sc = SIZE_COLORS.get(size, {"fill": (255, 255, 255), "text": (0, 0, 0)})
            pdf.set_fill_color(*sc["fill"])
            pdf.set_text_color(*sc["text"])
            pdf.cell(20, 5, sanitize(size), border=1, fill=True, align="C")
            pdf.set_text_color(30, 30, 30)
            pdf.cell(18, 5, sanitize(a.get("surface", "")), border=1, align="C")
            wall = "Y" if a.get("wall_available") else "N"
            pdf.cell(10, 5, wall, border=1, align="C")
            notes = a.get("notes", "")[:60]
            pdf.cell(92, 5, sanitize(notes), border=1)
            pdf.ln()

            if pdf.get_y() > 270:
                pdf.add_page()
                # Re-draw header
                pdf.set_font("Helvetica", "B", 7)
                pdf.set_fill_color(243, 244, 246)
                pdf.set_text_color(55, 65, 81)
                pdf.cell(10, 5, "VP", border=1, fill=True, align="C")
                pdf.cell(15, 5, "IDW", border=1, fill=True, align="C")
                pdf.cell(12, 5, "Score", border=1, fill=True, align="C")
                pdf.cell(20, 5, "Size", border=1, fill=True, align="C")
                pdf.cell(18, 5, "Surface", border=1, fill=True, align="C")
                pdf.cell(10, 5, "Wall", border=1, fill=True, align="C")
                pdf.cell(92, 5, "Notes", border=1, fill=True)
                pdf.ln()

    # ─── ZONING & PLANNING ────────────────────────────────────────
    if zoning_analysis:
        pdf.add_page()
        pdf.section_title("Zoning & Planning Analysis")
        for field in ["zone_classification", "applicable_regulations",
                      "permits_required", "approval_timeline",
                      "special_plan_restrictions"]:
            val = zoning_analysis.get(field, "")
            if val:
                label = field.replace("_", " ").title()
                pdf.kv(label, "")
                pdf.body_text(val)
                pdf.ln(1)

    # ─── METHODOLOGY ──────────────────────────────────────────────
    pdf.add_page()
    pdf.section_title("Methodology")

    pdf.sub_title("ML Heatmap")
    pdf.body_text(
        "XGBoost classifier (AUC 0.94) trained on 1,189 existing bpost locker locations "
        "across Belgium. 3.1M grid points at 100m resolution. Features include transit "
        "access, commercial density, pedestrian infrastructure, and demographics."
    )

    pdf.sub_title("Street Corridor")
    pdf.body_text(
        "Dijkstra shortest-path routing on OpenStreetMap street graph between ML hot-zone "
        "anchors (score >= 0.60). BFS expansion covers all hot-zone streets not on the "
        "main path. Viewpoints sampled every 22m along the corridor."
    )

    pdf.sub_title("Pass 1: Screening")
    pdf.body_text(
        "Every 3rd viewpoint assessed with Claude Sonnet via alternating left/right "
        "perpendicular Street View images (FOV 90 degrees). Smart download with fallback "
        "strategies for blocked or unavailable views. Scores >= 4/10 flagged as interesting."
    )

    pdf.sub_title("Pass 2: Multi-Angle Detail")
    pdf.body_text(
        "Viewpoints near interesting locations captured from multiple angles: standard L/R "
        "(FOV 90), wide context (FOV 120), tight detail (FOV 60), look-toward from adjacent "
        "viewpoints, and junction branches. All images for each candidate group sent to "
        "Claude Opus in a single call for cross-referenced assessment."
    )

    pdf.sub_title("Enrichment")
    pdf.body_text(
        "Top candidates enriched with: reverse-geocoded address (Nominatim), regional "
        "zoning classification (BruGIS/SPW/Geopunt), heritage zone checks, OSM business "
        "data, physical infrastructure (footway widths, surfaces, obstacles), satellite "
        "imagery, and location context (transit, commerce, parking, pedestrian)."
    )

    pdf.sub_title("Final Assessment")
    pdf.body_text(
        "Claude Opus 4.6 with extended thinking assesses all enrichment data plus "
        "street-level and satellite imagery. Produces feasibility verdicts, visual "
        "observations, contact details, and a final recommendation with next steps. "
        "All 4 locker sizes (Compact 0.6m, Standard 1.2m, Large 2.4m, XL 4.8m) "
        "are considered."
    )

    pdf.sub_title("Locker Sizes")
    pdf.set_font("Helvetica", "", 8)
    pdf.set_fill_color(243, 244, 246)
    pdf.cell(25, 5, "Size", border=1, fill=True, align="C")
    pdf.cell(18, 5, "Width", border=1, fill=True, align="C")
    pdf.cell(18, 5, "Depth", border=1, fill=True, align="C")
    pdf.cell(18, 5, "Height", border=1, fill=True, align="C")
    pdf.cell(25, 5, "Clearance", border=1, fill=True, align="C")
    pdf.ln()
    sizes = sv_report.get("locker_sizes_reference", {
        "compact": {"w": 0.6, "d": 0.7, "h": 2.0, "clearance": 1.2},
        "standard": {"w": 1.2, "d": 0.7, "h": 2.0, "clearance": 1.5},
        "large": {"w": 2.4, "d": 0.7, "h": 2.0, "clearance": 1.5},
        "xl": {"w": 4.8, "d": 0.7, "h": 2.0, "clearance": 2.0},
    })
    for name, spec in sizes.items():
        sc = SIZE_COLORS.get(name, {"fill": (255, 255, 255), "text": (0, 0, 0)})
        pdf.set_fill_color(*sc["fill"])
        pdf.set_text_color(*sc["text"])
        pdf.set_font("Helvetica", "B", 8)
        pdf.cell(25, 5, name.upper(), border=1, fill=True, align="C")
        pdf.set_text_color(30, 30, 30)
        pdf.set_font("Helvetica", "", 8)
        pdf.cell(18, 5, f"{spec['w']}m", border=1, align="C")
        pdf.cell(18, 5, f"{spec['d']}m", border=1, align="C")
        pdf.cell(18, 5, f"{spec['h']}m", border=1, align="C")
        pdf.cell(25, 5, f"{spec['clearance']}m", border=1, align="C")
        pdf.ln()

    # Save
    output_path = report_dir / "sv_report.pdf"
    pdf.output(str(output_path))
    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"\nPDF generated: {output_path} ({size_mb:.1f} MB, {pdf.page_no()} pages)")
    return output_path


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/sv_report_to_pdf.py <report_dir>")
        sys.exit(1)

    report_dir = Path(sys.argv[1])
    if not report_dir.is_dir():
        # Maybe they passed a JSON file — use its parent
        if report_dir.suffix == ".json":
            report_dir = report_dir.parent
        else:
            print(f"Error: {report_dir} is not a directory", file=sys.stderr)
            sys.exit(1)

    generate_sv_report_pdf(report_dir)


if __name__ == "__main__":
    main()
