#!/usr/bin/env python3
"""
Convert a local analysis JSON report to a formatted PDF.

Usage:
  python3 scripts/report_to_pdf.py data/local_reports/Ixelles_0.5km_20260228/report.json
"""

import json
import sys
from pathlib import Path
from fpdf import FPDF


def sanitize(text):
    """Replace problematic Unicode chars with ASCII equivalents."""
    return (str(text)
            .replace("\u2014", "-")
            .replace("\u2013", "-")
            .replace("\u2018", "'")
            .replace("\u2019", "'")
            .replace("\u201c", '"')
            .replace("\u201d", '"')
            .replace("\u2026", "..."))


class ReportPDF(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(100, 100, 100)
        self.cell(0, 6, sanitize("bbox Coverage Tool - Ground Truth Agent"), align="R")
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
        self.cell(45, 5, sanitize(key + ":"))
        self.set_text_color(30, 30, 30)
        self.set_font("Helvetica", "B" if bold_value else "", 9)
        self.cell(0, 5, sanitize(str(value)))
        self.ln(5)

    def score_bar(self, score, max_score, width=30):
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

    def verdict_badge(self, verdict):
        """Draw a colored feasibility verdict badge."""
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


def generate_pdf(report_path):
    report_path = Path(report_path)
    with open(report_path) as f:
        report = json.load(f)

    meta = report["meta"]
    candidates = report["candidates"]
    zoning = report["zoning_research"]
    summary = report["summary"]
    ai = report.get("ai_enrichment", {})

    # Resolve image directory relative to report
    img_dir = report_path.parent / "images"

    pdf = ReportPDF("P", "mm", "A4")
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    # --- Title page ---
    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(17, 24, 39)
    pdf.cell(0, 12, "Local Analysis Report")
    pdf.ln(10)
    pdf.set_font("Helvetica", "", 12)
    pdf.set_text_color(75, 85, 99)
    area = meta["area_name"].replace("---", " / ").replace("-", " ").replace("_", " ")
    pdf.cell(0, 8, sanitize(area))
    pdf.ln(8)
    center = meta["center"]

    # Sector summary (demographics + competition)
    ss = meta.get("sector_summary", {})
    if ss:
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(55, 65, 81)
        quadrant = ss.get("quadrant", "").replace("_", " ").title()
        parts = [
            f"Sector {ss.get('sector', '?')}",
            ss.get("zone", "").title(),
            f"Pop: {ss.get('population', 0):,}",
            f"Demand: {ss.get('demand', 0):,.0f}",
            f"Competitors: {ss.get('competitor_count', 0)}",
            f"Gap: {ss.get('coverage_gap', 0):.1f}",
        ]
        if quadrant:
            parts.append(quadrant)
        pdf.cell(0, 5, sanitize(" | ".join(parts)))
        pdf.ln(4)
        ops = ss.get("operators", [])
        if ops:
            pdf.set_font("Helvetica", "I", 8)
            pdf.set_text_color(107, 114, 128)
            pdf.cell(0, 4, sanitize(f"Operators: {', '.join(ops)}"))
            pdf.ln(4)
    pdf.ln(4)

    # Overview satellite map
    overview_path = img_dir / "overview_satellite.png"
    if overview_path.exists():
        y_before = pdf.get_y()
        pdf.image(str(overview_path), x=pdf.l_margin, y=y_before, w=170, h=100)
        pdf.set_y(y_before + 102)
        pdf.set_font("Helvetica", "I", 7)
        pdf.set_text_color(107, 114, 128)
        pdf.cell(170, 4, "Overview - candidate locations", align="C")
        pdf.ln(6)

    # Candidate summary list (ranked)
    for cand in candidates:
        site = cand.get("suggested_site", {})
        verdict = cand.get("physical_feasibility", {}).get("verdict", "")
        score = cand.get("site_score") or cand.get("gt_score", 0)
        site_name = site.get("name", "Unknown")
        site_type = site.get("type", "")
        dist = site.get("dist_m", 0)

        # Number
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(255, 140, 0)
        pdf.cell(8, 5, f"#{cand['id']}")

        # Name + type + distance
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(55, 5, sanitize(f"{site_name} ({site_type}, {dist}m)"))

        # Verdict (color-coded)
        if verdict:
            v = verdict.lower()
            if "feasible" in v and "not" not in v:
                pdf.set_text_color(22, 163, 74)
            elif "marginal" in v:
                pdf.set_text_color(161, 98, 7)
            else:
                pdf.set_text_color(220, 38, 38)
            pdf.set_font("Helvetica", "", 8)
            pdf.cell(25, 5, sanitize(verdict))

        # Score
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(75, 85, 99)
        pdf.cell(0, 5, f"Score: {score}")
        pdf.ln(5)

    pdf.ln(4)

    # Analysis Parameters
    pdf.section_title("Analysis Parameters")
    pdf.kv("Center", f"{center[0]:.5f}, {center[1]:.5f}")
    pdf.kv("Radius", f"{meta['radius_km']} km")
    pdf.kv("Travel time", f"{meta['travel_time']} min")
    pdf.kv("Generated", meta["generated"])
    pdf.kv("Baseline lockers", f"{meta['baseline_lockers']:,}")
    pdf.kv("Approved lockers", str(meta.get("approved_lockers", 0)))
    if meta.get("target_sector"):
        pdf.kv("Target sector", meta["target_sector"])
    pdf.ln(4)

    # Recommendation (v2 structured)
    rec = ai.get("recommendation", {})
    rec_text = ai.get("overall_recommendation", "")
    if rec or rec_text:
        pdf.section_title("Recommendation")
        if rec.get("winner_id"):
            pdf.set_font("Helvetica", "B", 11)
            pdf.set_text_color(22, 163, 74)
            pdf.cell(0, 7, sanitize(f"Recommended: Candidate #{rec['winner_id']}"))
            pdf.ln(7)
        reasoning = rec.get("reasoning", rec_text)
        if reasoning:
            pdf.set_font("Helvetica", "", 10)
            pdf.set_text_color(30, 30, 30)
            pdf.multi_cell(0, 5, sanitize(reasoning))
            pdf.ln(3)
        next_steps = rec.get("next_steps", [])
        if next_steps:
            pdf.sub_title("Next Steps")
            for i, step in enumerate(next_steps, 1):
                pdf.set_font("Helvetica", "", 9)
                pdf.set_text_color(30, 30, 30)
                pdf.cell(8, 5, f"{i}.")
                pdf.multi_cell(0, 5, sanitize(step))
                pdf.ln(1)
    pdf.ln(4)

    # --- Candidate pages (one per candidate) ---
    for cand in candidates:
        pdf.add_page()

        bd = cand["breakdown"]
        score = cand.get("site_score") or cand.get("gt_score", 0)
        explanations = cand.get("score_explanations", {})

        # Color coding
        if score >= 70:
            pdf.set_fill_color(220, 252, 231)
            badge_color = (22, 163, 74)
        elif score >= 40:
            pdf.set_fill_color(254, 249, 195)
            badge_color = (161, 98, 7)
        else:
            pdf.set_fill_color(254, 226, 226)
            badge_color = (220, 38, 38)

        # Score badge + header
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(*badge_color)
        pdf.cell(24, 8, sanitize(f"Score {score}"), border=0, fill=True, align="C")
        pdf.set_text_color(17, 24, 39)
        pdf.cell(4, 8, "")
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, sanitize(f"#{cand['id']}  {cand['sector']}  ({cand['source']})"))
        pdf.ln(9)

        # Address
        address = cand.get("address", "")
        if address:
            pdf.set_font("Helvetica", "", 9)
            pdf.set_text_color(75, 85, 99)
            pdf.cell(0, 5, sanitize(address))
            pdf.ln(5)

        # Zoning classification badge (from API data)
        zoning_data = cand.get("zoning_data", {})
        zone_name = zoning_data.get("zone_name_fr") or zoning_data.get("zone_name_nl")
        if zone_name:
            pdf.set_font("Helvetica", "I", 8)
            pdf.set_text_color(100, 100, 140)
            zone_type = zoning_data.get("zone_type", "")
            pdf.cell(0, 5, sanitize(f"Zoning: {zone_name} ({zone_type})"))
            pdf.ln(5)

        # Suggested placement site
        site = cand.get("suggested_site")
        if site:
            pdf.set_font("Helvetica", "B", 10)
            pdf.set_text_color(22, 101, 52)
            pdf.cell(0, 6, sanitize(
                f"Suggested site: {site['name']} ({site['type']}, {site['dist_m']}m from target coordinates)"))
            pdf.ln(7)

        # Commentary (short summary, right after suggested site)
        commentary = cand.get("commentary", "")
        if commentary:
            pdf.set_font("Helvetica", "", 9)
            pdf.set_text_color(30, 30, 30)
            pdf.multi_cell(0, 5, sanitize(commentary))
            pdf.ln(3)

        # Physical Feasibility (v2 — detailed analysis)
        feasibility = cand.get("physical_feasibility", {})
        verdict = feasibility.get("verdict", "")
        if verdict:
            pdf.sub_title("Physical Feasibility")
            pdf.verdict_badge(verdict)
            pdf.ln(7)
            for fkey, flabel in [
                ("footpath_assessment", "Footpath"),
                ("space_assessment", "Space"),
                ("accessibility", "Access"),
                ("visibility_traffic", "Visibility"),
                ("obstacles", "Obstacles"),
            ]:
                fval = feasibility.get(fkey, "")
                if fval:
                    pdf.set_font("Helvetica", "B", 8)
                    pdf.set_text_color(75, 85, 99)
                    pdf.cell(20, 4, sanitize(flabel + ":"))
                    pdf.set_font("Helvetica", "", 8)
                    pdf.set_text_color(30, 30, 30)
                    pdf.multi_cell(0, 4, sanitize(fval))
                    pdf.ln(1)
            pdf.ln(2)

        # Images (satellite + annotated street view)
        cand_id = str(cand["id"])
        sat_path = img_dir / f"candidate_{cand_id}_satellite.png"
        # Prefer annotated street view if available
        sv_annotated = img_dir / f"candidate_{cand_id}_streetview_annotated.png"
        sv_path = sv_annotated if sv_annotated.exists() else img_dir / f"candidate_{cand_id}_streetview.png"

        has_sat = sat_path.exists()
        has_sv = sv_path.exists()

        if has_sat and has_sv:
            y_before = pdf.get_y()
            pdf.image(str(sat_path), x=pdf.l_margin, y=y_before, w=85, h=55)
            pdf.image(str(sv_path), x=pdf.l_margin + 90, y=y_before, w=85, h=55)
            pdf.set_y(y_before + 57)
            pdf.set_font("Helvetica", "I", 7)
            pdf.set_text_color(107, 114, 128)
            pdf.cell(85, 4, "Satellite view", align="C")
            pdf.cell(5, 4, "")
            sv_label = "Street View (annotated)" if sv_annotated.exists() else "Street View"
            pdf.cell(85, 4, sv_label, align="C")
            pdf.ln(6)
        elif has_sat:
            y_before = pdf.get_y()
            pdf.image(str(sat_path), x=pdf.l_margin, y=y_before, w=120, h=75)
            pdf.set_y(y_before + 77)
            pdf.set_font("Helvetica", "I", 7)
            pdf.set_text_color(107, 114, 128)
            pdf.cell(120, 4, "Satellite view", align="C")
            pdf.ln(6)

        # AI observations (data-driven analysis, not visual evidence)
        observations = cand.get("visual_observations", [])
        if observations:
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_text_color(55, 65, 81)
            pdf.cell(0, 5, "AI Observations:")
            pdf.ln(5)
            for i, obs in enumerate(observations, 1):
                # Number + label on its own line
                pdf.set_font("Helvetica", "B", 7)
                pdf.set_text_color(255, 140, 0)
                pdf.cell(6, 4, f"{i}.")
                pdf.set_font("Helvetica", "B", 7)
                pdf.set_text_color(55, 65, 81)
                pdf.cell(0, 4, sanitize(obs.get("label", "")))
                pdf.ln(4)
                # Description indented below
                desc = obs.get("description", "")
                if desc:
                    pdf.set_x(pdf.l_margin + 6)
                    pdf.set_font("Helvetica", "", 7)
                    pdf.set_text_color(107, 114, 128)
                    pdf.multi_cell(0, 4, sanitize(desc))
                pdf.ln(2)
            pdf.ln(2)

        # Metrics row
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(75, 85, 99)
        metrics = []
        if cand.get("pop_gain"):
            metrics.append(f"Pop gain: {cand['pop_gain']:,}")
        metrics.append(f"Nearest locker: {cand['nearest_existing_m']}m")
        metrics.append(f"Competitors: {cand['competitors_nearby']}")
        metrics.append(f"Coords: {cand['lat']:.5f}, {cand['lng']:.5f}")
        pdf.cell(0, 4, "  |  ".join(metrics))
        pdf.ln(6)

        # Score breakdown bars with explanation notes
        for label, score_val, max_s, expl_key in [
            ("Transit", bd["transit"], 25, "transit"),
            ("Commerce", bd["commerce"], 30, "commerce"),
            ("Building", bd["building"], 20, "building"),
            ("Pedestrian", bd["pedestrian"], 15, "pedestrian"),
            ("Parking", bd["parking"], 10, "parking"),
        ]:
            pdf.set_font("Helvetica", "", 8)
            pdf.set_text_color(75, 85, 99)
            pdf.cell(22, 5, label)
            pdf.score_bar(score_val, max_s, width=30)
            explanation = explanations.get(expl_key, "")
            if explanation:
                pdf.set_font("Helvetica", "I", 7)
                pdf.set_text_color(107, 114, 128)
                max_chars = 70
                if len(explanation) > max_chars:
                    explanation = explanation[:max_chars - 3] + "..."
                pdf.cell(0, 5, sanitize(explanation))
            pdf.ln(5)

        pdf.ln(2)

        # Contact Details (v2 structured)
        cd = cand.get("contact_details", {})
        if cd:
            pdf.sub_title("Contact Details")
            for cd_key, cd_label in [
                ("site_type", "Site type"),
                ("business_name", "Business"),
                ("parent_company", "Parent company"),
                ("contact_approach", "Approach"),
                ("phone_website", "Phone/Website"),
                ("commune_authority", "Commune authority"),
            ]:
                cd_val = cd.get(cd_key, "")
                if cd_val:
                    pdf.set_font("Helvetica", "B", 8)
                    pdf.set_text_color(75, 85, 99)
                    pdf.cell(30, 4, sanitize(cd_label + ":"))
                    pdf.set_font("Helvetica", "", 8)
                    pdf.set_text_color(30, 30, 30)
                    pdf.multi_cell(0, 4, sanitize(cd_val))
                    pdf.ln(1)
            pdf.ln(2)
        else:
            # Fallback to v1 contact_info string
            contact = cand.get("contact_info", "")
            if contact:
                pdf.set_font("Helvetica", "B", 8)
                pdf.set_text_color(55, 65, 81)
                pdf.cell(18, 5, "Contact:")
                pdf.set_font("Helvetica", "", 8)
                pdf.set_text_color(30, 30, 30)
                pdf.multi_cell(0, 5, sanitize(contact))
                pdf.ln(2)

        # Nearby POIs
        pois = cand.get("nearby_pois", [])
        if pois:
            pdf.set_font("Helvetica", "I", 7)
            pdf.set_text_color(107, 114, 128)
            poi_strs = [f"{p['name']} ({p['type']}, {p['dist_m']}m)" for p in pois[:5]]
            pdf.cell(0, 4, sanitize("Nearby: " + ", ".join(poi_strs)))
            pdf.ln(4)

    # --- Zoning & Planning Research ---
    pdf.add_page()
    pdf.section_title("Zoning & Planning Research")
    pdf.kv("Commune", zoning.get("commune", ""), bold_value=True)
    pdf.kv("Region", zoning.get("region", ""))
    pdf.kv("Planning portal", zoning.get("planning_portal", ""))
    pdf.kv("Permits portal", zoning.get("permits_portal", "N/A"))
    pdf.ln(4)

    # Zoning analysis (v2 structured from AI)
    za = ai.get("zoning_analysis", {})
    if za:
        pdf.sub_title("Zoning Compliance Analysis")
        for za_key, za_label in [
            ("zone_classification", "Zone classification"),
            ("applicable_regulations", "Applicable regulations"),
            ("permits_required", "Permits required"),
            ("approval_timeline", "Approval timeline"),
            ("special_plan_restrictions", "Special plan restrictions"),
        ]:
            za_val = za.get(za_key, "")
            if za_val:
                pdf.set_font("Helvetica", "B", 9)
                pdf.set_text_color(75, 85, 99)
                pdf.cell(45, 5, sanitize(za_label + ":"))
                pdf.set_font("Helvetica", "", 9)
                pdf.set_text_color(30, 30, 30)
                pdf.multi_cell(0, 5, sanitize(za_val))
                pdf.ln(2)
        pdf.ln(4)
    else:
        # Fallback to v1 zoning_findings string
        zoning_findings = ai.get("zoning_findings", "")
        if zoning_findings:
            pdf.sub_title("Zoning Analysis")
            pdf.set_font("Helvetica", "", 9)
            pdf.set_text_color(30, 30, 30)
            pdf.multi_cell(0, 5, sanitize(zoning_findings))
            pdf.ln(4)

    # --- Scoring Methodology ---
    pdf.ln(6)
    pdf.section_title("Scoring Methodology")

    col_widths = [25, 15, 12, 120]
    headers = ["Criterion", "Weight", "Max", "Logic"]
    rows = [
        ("Transit", "25%", "25", "Bus stops in 300m (cap 5, x3) + rail in 500m (x10)"),
        ("Commerce", "30%", "30", "Shops/amenities in 300m (cap 15, x2)"),
        ("Building", "20%", "20", "Residential x0.4 + commercial x0.6, relative to avg"),
        ("Pedestrian", "15%", "15", "Sidewalks/crossings in 150m (cap 8, x1.875)"),
        ("Parking", "10%", "10", "Within 200m: 0->0, 1->7, 2+->10"),
    ]

    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(243, 244, 246)
    pdf.set_text_color(30, 30, 30)
    for h, w in zip(headers, col_widths):
        pdf.cell(w, 6, h, border=1, fill=True, align="C")
    pdf.ln()

    pdf.set_font("Helvetica", "", 8)
    for row in rows:
        for j, (cell, w) in enumerate(zip(row, col_widths)):
            pdf.cell(w, 5, cell, border=1, align="C" if j < 3 else "L")
        pdf.ln()

    # --- Appendix A: Research Artifacts ---
    pdf.add_page()
    pdf.section_title("Appendix A: Research Artifacts")
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(107, 114, 128)
    pdf.cell(0, 4, "Factual data collected from APIs during analysis (not AI-generated)")
    pdf.ln(8)

    for cand in candidates:
        cid = cand["id"]
        site = cand.get("suggested_site", {})
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(30, 58, 138)
        pdf.cell(0, 7, sanitize(f"Candidate #{cid}: {site.get('name', 'Unknown')}"))
        pdf.ln(7)

        # Zoning data
        zd = cand.get("zoning_data", {})
        if zd:
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_text_color(55, 65, 81)
            pdf.cell(0, 5, "Zoning Classification")
            pdf.ln(4)
            for zk, zl in [("zone_type", "Zone type"), ("zone_name_fr", "Name (FR)"),
                           ("zone_name_nl", "Name (NL)"), ("regulation_url", "Regulation")]:
                zv = zd.get(zk, "")
                if zv:
                    pdf.set_font("Helvetica", "", 7)
                    pdf.set_text_color(75, 85, 99)
                    pdf.cell(25, 4, sanitize(zl + ":"))
                    pdf.set_text_color(30, 30, 30)
                    if zk == "regulation_url":
                        pdf.set_text_color(30, 80, 180)
                    pdf.cell(0, 4, sanitize(str(zv)))
                    pdf.ln(4)

        # Heritage zones
        hz = cand.get("heritage_zones", [])
        if hz:
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_text_color(55, 65, 81)
            pdf.cell(0, 5, "Heritage Zones")
            pdf.ln(4)
            for h in hz:
                pdf.set_font("Helvetica", "", 7)
                pdf.set_text_color(30, 30, 30)
                txt = h.get("type_fr", h.get("type_nl", ""))
                if txt:
                    pdf.cell(0, 4, sanitize(f"- {txt}"))
                    pdf.ln(4)

        # Special plans
        sp = cand.get("special_plans", [])
        if sp:
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_text_color(55, 65, 81)
            pdf.cell(0, 5, "Special Plans")
            pdf.ln(4)
            for s in sp:
                pdf.set_font("Helvetica", "", 7)
                pdf.set_text_color(30, 30, 30)
                pdf.cell(0, 4, sanitize(f"{s.get('name', '')} ({s.get('date', '')}) - {s.get('status', '')}"))
                pdf.ln(4)

        # Physical context
        pc = cand.get("physical_context", {})
        if pc:
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_text_color(55, 65, 81)
            pdf.cell(0, 5, "Physical Infrastructure (Overpass API)")
            pdf.ln(4)
            pdf.set_font("Helvetica", "", 7)
            pdf.set_text_color(30, 30, 30)
            for fw in pc.get("footway_widths", []):
                pdf.cell(0, 4, sanitize(f"Footway: {fw.get('name', '?')} - width: {fw.get('width', '?')}m"))
                pdf.ln(4)
            for sf in pc.get("surfaces", []):
                pdf.cell(0, 4, sanitize(f"Surface: {sf.get('name', '?')} - {sf.get('surface', '?')}"))
                pdf.ln(4)
            for sw in pc.get("sidewalk_streets", []):
                pdf.cell(0, 4, sanitize(f"Sidewalk: {sw.get('name', '?')} - {sw.get('sidewalk', '?')}"))
                pdf.ln(4)
            obstacles = pc.get("obstacles", [])
            if obstacles:
                obs_strs = [f"{o.get('type', '?')}" + (f" ({o['name']})" if o.get("name") else "")
                            for o in obstacles]
                pdf.cell(0, 4, sanitize(f"Obstacles: {', '.join(obs_strs)}"))
                pdf.ln(4)

        # Business details
        bd = cand.get("business_details", {})
        if bd and any(bd.values()):
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_text_color(55, 65, 81)
            pdf.cell(0, 5, "Business Data (OSM)")
            pdf.ln(4)
            pdf.set_font("Helvetica", "", 7)
            pdf.set_text_color(30, 30, 30)
            for bk, bl in [("brand", "Brand"), ("brand_wikidata", "Wikidata"),
                           ("opening_hours", "Hours"), ("phone", "Phone"), ("website", "Website")]:
                bv = bd.get(bk, "")
                if bv:
                    pdf.cell(20, 4, sanitize(bl + ":"))
                    if bk == "brand_wikidata":
                        pdf.set_text_color(30, 80, 180)
                        pdf.cell(0, 4, sanitize(f"https://www.wikidata.org/wiki/{bv}"))
                        pdf.set_text_color(30, 30, 30)
                    elif bk == "website":
                        pdf.set_text_color(30, 80, 180)
                        pdf.cell(0, 4, sanitize(bv))
                        pdf.set_text_color(30, 30, 30)
                    else:
                        pdf.cell(0, 4, sanitize(bv))
                    pdf.ln(4)

        pdf.ln(6)

    # Planning portals
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(55, 65, 81)
    pdf.cell(0, 5, "Planning Portals")
    pdf.ln(4)
    pdf.set_font("Helvetica", "", 7)
    pdf.set_text_color(30, 80, 180)
    for pk, pl in [("planning_portal", "Planning"), ("permits_portal", "Permits")]:
        pv = zoning.get(pk, "")
        if pv:
            pdf.cell(20, 4, sanitize(pl + ":"))
            pdf.cell(0, 4, sanitize(pv))
            pdf.ln(4)

    # --- Appendix B: AI Analysis Methodology (final page) ---
    pdf.add_page()
    pdf.section_title("Appendix B: AI Analysis Methodology")

    ai_steps = [
        ("Model", "Claude Opus 4.6 with adaptive extended thinking"),
        ("Vision", "Satellite imagery (Google Maps / ESRI fallback) and Google Street View "
                    "images are sent as base64 content blocks for visual analysis"),
        ("Input Data",
         "Sector-level demographics from centroids.json (population, density, demand score), "
         "competitive coverage from competitive_coverage.json (competitor count, coverage gap, "
         "operators), and strategic quadrant classification. For supermarket POI placements, "
         "the nearest statistical sector is resolved automatically"),
        ("Data Sources",
         "Regional zoning APIs (BruGIS WFS for Brussels, SPW ArcGIS for Wallonia, "
         "Geopunt WFS for Flanders), OSM Overpass for physical infrastructure "
         "(footway widths, surfaces, obstacles), business contacts, and transit data"),
        ("Physical Feasibility",
         "Each candidate is assessed for footpath width (min 1.5m clear), wall/frontage space, "
         "accessibility (level ground, no barriers), visibility/foot traffic, and nearby obstacles. "
         "Verdict: Feasible / Marginal / Not feasible"),
        ("Zoning Compliance",
         "Zone classification from regional APIs, applicable regulations "
         "(COBAT / CoDT / VCRO), required permits, and estimated approval timeline"),
        ("Contact Research",
         "Business identification from OSM tags + Nominatim. Chain stores mapped to corporate "
         "real estate departments. Public sites mapped to commune urbanisme services"),
        ("Recommendation",
         "Comparative analysis across all candidates using physical feasibility as primary filter, "
         "with strategic positioning and zoning compliance as secondary factors"),
        ("Cost Controls",
         "Local pre-filtering scores all nearby POIs before Claude enrichment. "
         "Only top candidates are sent per iteration (default: 4). "
         "Configurable cost cap (--max-cost, default $5) and iteration limit (--max-iterations, default 10) "
         "halt enrichment when budget or search limits are reached"),
    ]
    for label, desc in ai_steps:
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(55, 65, 81)
        pdf.cell(35, 5, sanitize(label + ":"))
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(30, 30, 30)
        pdf.multi_cell(0, 4, sanitize(desc))
        pdf.ln(2)

    # Save
    out_path = str(report_path).replace("report.json", "report.pdf")
    if out_path == str(report_path):
        out_path = str(report_path).replace(".json", ".pdf")
    pdf.output(out_path)
    print(f"PDF saved to: {out_path}")
    print(f"File size: {Path(out_path).stat().st_size / 1024:.1f} KB")
    return out_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/report_to_pdf.py <report.json>")
        sys.exit(1)
    generate_pdf(sys.argv[1])
