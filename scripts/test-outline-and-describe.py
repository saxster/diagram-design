#!/usr/bin/env python3
"""Tests for font_metrics.py, export_svg.py --outline-text, and describe_diagram.py."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills/diagram-design/scripts"
ASSETS = ROOT / "skills/diagram-design/assets"


def _load(name: str):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_mod_{name}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"test_mod_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


class FontMetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fm = _load("font_metrics")

    def test_bundled_faces_load_and_kern(self) -> None:
        self.assertTrue(self.fm.HAS_FONTTOOLS)
        sans = self.fm.resolve_face("'Geist', sans-serif", 600)
        mono = self.fm.resolve_face("'Geist Mono', monospace", 400)
        serif = self.fm.resolve_face("'Instrument Serif', serif", 400, "italic")
        assert sans is not None and mono is not None and serif is not None
        self.assertEqual(sans.label, "Geist SemiBold")
        self.assertEqual(mono.label, "Geist Mono Regular")
        self.assertEqual(serif.label, "Instrument Serif Italic")

        # GPOS kerning reduces AV width compared to sum of A + V.
        av = sans.measure("AV", 12)
        a = sans.measure("A", 12)
        v = sans.measure("V", 12)
        assert av is not None and a is not None and v is not None
        self.assertLess(av, a + v)

    def test_monotonic_width_and_glyph_support(self) -> None:
        sans = self.fm.resolve_face("Geist", 400)
        assert sans is not None
        prev = 0.0
        cur = ""
        for ch in "Cloudflare Edge":
            cur += ch
            w = sans.measure(cur, 12)
            assert w is not None
            self.assertGreaterEqual(w, prev)
            prev = w
        self.assertFalse(sans.can_render("결제"))
        self.assertFalse(sans.can_render("✓ PASS"))
        self.assertTrue(sans.can_render("Orders API v1 → v2"))
        d = sans.outline("API", 12, 10, 20)
        assert d is not None
        self.assertTrue(d.startswith("M"))


class OutlineSvgExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.es = _load("export_svg")
        cls.dd = _load("describe_diagram")

    def test_default_export_unchanged_differential(self) -> None:
        src = ASSETS / "example-architecture.html"
        html = src.read_text(encoding="utf-8")
        plain_a = self.es.export_svg_document(html, src)
        plain_b = self.es.export_svg_document(html, src, outline_text=False)
        self.assertEqual(plain_a, plain_b)
        self.assertIn("<text", plain_a)
        self.assertNotIn("data-outlined-text", plain_a)

    def test_inline_styled_diagram_outlines_all_text_without_network(self) -> None:
        src = ASSETS / "example-architecture.html"
        svg = self.es.export_svg_document(src.read_text(encoding="utf-8"), src, outline_text=True)
        root = ET.fromstring(svg)
        self.assertEqual([e for e in root.iter() if e.tag.endswith("}text")], [])
        outlined = [e for e in root.iter() if e.get("data-outlined-text") == "true"]
        self.assertEqual(len(outlined), 32)
        self.assertNotIn("fonts.googleapis.com", svg)
        # Round-trip semantic equality against source HTML.
        html_summary = self.dd.describe_document(src.read_text(encoding="utf-8"))
        svg_summary = self.dd.describe_document(svg)
        self.assertEqual([n.label for n in html_summary.nodes], [n.label for n in svg_summary.nodes])
        self.assertEqual(
            [(c.source, c.target, c.label) for c in html_summary.connections],
            [(c.source, c.target, c.label) for c in svg_summary.connections],
        )

    def test_css_styled_vars_and_shorthand_resolve_colors(self) -> None:
        src = ASSETS / "example-loop.html"
        svg = self.es.export_svg_document(src.read_text(encoding="utf-8"), src, outline_text=True)
        root = ET.fromstring(svg)
        self.assertEqual([e for e in root.iter() if e.tag.endswith("}text")], [])
        self.assertNotIn("fonts.googleapis.com", svg)
        fills = {e.get("fill") for e in root.iter() if e.tag.endswith("}path") and e.get("fill")}
        self.assertIn("#bf4520", fills)  # focal-name var(--accent)
        self.assertIn("#2d3142", fills)  # node-name var(--ink)
        self.assertIn("#f5f5f5", fills)  # hub-name var(--paper)
        hub_sub = next(e for e in root.iter() if "hub-sub" in e.get("class", ""))
        self.assertEqual(hub_sub.get("opacity"), "0.72")

    def test_rotated_and_multi_run_tspan_and_unmapped_fallback(self) -> None:
        bar = ASSETS / "example-bar.html"
        bar_svg = self.es.export_svg_document(bar.read_text(encoding="utf-8"), bar, outline_text=True)
        bar_root = ET.fromstring(bar_svg)
        rotated = [e for e in bar_root.iter() if (e.get("transform") or "").startswith("rotate(")]
        self.assertEqual(len(rotated), 1)
        self.assertEqual(rotated[0].get("data-outlined-text"), "true")

        quad = ASSETS / "example-quadrant-consultant.html"
        quad_svg = self.es.export_svg_document(quad.read_text(encoding="utf-8"), quad, outline_text=True)
        quad_root = ET.fromstring(quad_svg)
        self.assertEqual([e for e in quad_root.iter() if e.tag.endswith("}text")], [])
        agent_group = next(
            g for g in quad_root.iter()
            if g.get("data-outlined-text") == "true"
            and any(c.tag.endswith("}title") and "Agent teams" in (c.text or "") for c in g)
        )
        child_fills = [c.get("fill") for c in agent_group if c.tag.endswith("}path")]
        self.assertIn("#bf4520", child_fills)
        self.assertIn("#2d3142", child_fills)

        # example-architecture-delta contains unmapped '⇢' in one legend label: keep only that <text>.
        delta = ASSETS / "example-architecture-delta.html"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            delta_svg = self.es.export_svg_document(
                delta.read_text(encoding="utf-8"), delta, outline_text=True
            )
        delta_root = ET.fromstring(delta_svg)
        kept = [e for e in delta_root.iter() if e.tag.endswith("}text")]
        self.assertEqual(len(kept), 1)
        self.assertIn("⇢", "".join(kept[0].itertext()))
        self.assertIn("kept 1 <text>", stderr.getvalue())


class DescribeDiagramTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dd = _load("describe_diagram")

    def test_cli_json_check_and_expect(self) -> None:
        src = ASSETS / "example-sequence-oauth.html"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.dd.main([str(src), "--json", "--check", "--expect", "Resource API"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["title"], "Bearer call with refresh")
        self.assertEqual(len(payload["nodes"]), 3)
        self.assertEqual(len(payload["connections"]), 8)

        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.html"
            bad.write_text(
                src.read_text(encoding="utf-8").replace("Bearer gate", "TBD protocol"),
                encoding="utf-8",
            )
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                bad_code = self.dd.main([str(bad), "--check"])
            self.assertEqual(bad_code, 1)
            self.assertIn("placeholder text", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
