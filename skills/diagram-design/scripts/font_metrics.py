#!/usr/bin/env python3
"""Exact fontTools glyph metrics, GPOS pair kerning, and SVG path outlining.

Shared by ``self_check.py`` (exact text-fit measurement) and ``export_svg.py``
(optional ``--outline-text`` portable SVG export). Degrades cleanly when
``fontTools`` is not installed so callers can fall back to budget heuristics.
"""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import Any

try:
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen
    from fontTools.ttLib import TTFont

    HAS_FONTTOOLS = True
except ImportError:  # pragma: no cover - optional runtime dependency
    SVGPathPen = None  # type: ignore[assignment]
    TransformPen = None  # type: ignore[assignment]
    TTFont = None  # type: ignore[assignment]
    HAS_FONTTOOLS = False


DEFAULT_FONT_DIR = Path(__file__).resolve().parent / "fonts"
SVG_NUM_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")

_SANS_FILES: dict[int, tuple[str, str]] = {
    400: ("Geist-Regular.ttf", "Geist Regular"),
    500: ("Geist-Medium.ttf", "Geist Medium"),
    600: ("Geist-SemiBold.ttf", "Geist SemiBold"),
}
_MONO_FILES: dict[int, tuple[str, str]] = {
    400: ("GeistMono-Regular.ttf", "Geist Mono Regular"),
    500: ("GeistMono-Medium.ttf", "Geist Mono Medium"),
    700: ("GeistMono-Bold.ttf", "Geist Mono Bold"),
}
_SERIF_FILES: dict[bool, tuple[str, str]] = {
    False: ("InstrumentSerif-Regular.ttf", "Instrument Serif Regular"),
    True: ("InstrumentSerif-Italic.ttf", "Instrument Serif Italic"),
}

_FACE_CACHE: dict[tuple[Path, str], "Face"] = {}


def fmt_svg_num(value: float) -> str:
    """Format an SVG path coordinate with at most two decimal places."""
    rendered = f"{value:.2f}".rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


def font_search_dirs() -> list[Path]:
    """Return candidate font directories in priority order."""
    dirs: list[Path] = []
    override = os.environ.get("DIAGRAM_DESIGN_FONT_DIR", "").strip()
    if override:
        dirs.append(Path(override).expanduser())
    dirs.append(DEFAULT_FONT_DIR)
    return dirs


def _find_font_file(filename: str) -> Path | None:
    for directory in font_search_dirs():
        candidate = directory / filename
        if candidate.is_file():
            return candidate
    return None


def classify_family(font_family: str | None) -> str:
    """Classify a CSS font-family stack into ``'mono'``, ``'serif'``, or ``'sans'``."""
    if not font_family:
        return "sans"
    for raw_token in font_family.split(","):
        token = raw_token.strip().strip("\"'").casefold()
        if not token:
            continue
        if "mono" in token or "courier" in token or "consolas" in token:
            return "mono"
        if "instrument serif" in token or ("serif" in token and "sans-serif" not in token):
            return "serif"
        if "geist" in token or "sans" in token or "system-ui" in token or "inter" in token:
            return "sans"
    return "sans"


def parse_weight(weight: str | int | None) -> int:
    """Parse a CSS font-weight into an integer 100..900 (default 400)."""
    if weight is None:
        return 400
    if isinstance(weight, int):
        return max(100, min(900, weight))
    cleaned = str(weight).strip().casefold()
    named = {
        "": 400,
        "normal": 400,
        "regular": 400,
        "medium": 500,
        "semibold": 600,
        "semi-bold": 600,
        "demibold": 600,
        "bold": 700,
        "bolder": 700,
        "lighter": 400,
    }
    if cleaned in named:
        return named[cleaned]
    try:
        return max(100, min(900, round(float(cleaned))))
    except ValueError:
        return 400


def parse_italic(style: str | None) -> bool:
    """Return True for italic or oblique CSS font-style values."""
    if not style:
        return False
    return style.strip().casefold() in {"italic", "oblique"}


def apply_text_transform(text: str, transform: str | None) -> str:
    """Apply CSS text-transform when known."""
    if not transform:
        return text
    mode = transform.strip().casefold()
    if mode == "uppercase":
        return text.upper()
    if mode == "lowercase":
        return text.lower()
    if mode == "capitalize":
        return text.title()
    return text


def parse_letter_spacing(value: str | None, font_size: float) -> float | None:
    """Parse CSS letter-spacing into px, or None if unreadable statically."""
    if value is None:
        return 0.0
    cleaned = value.strip().casefold()
    if cleaned in {"", "normal"}:
        return 0.0
    match = re.fullmatch(rf"({SVG_NUM_RE.pattern})\s*(em|px)?", cleaned)
    if match is None:
        return None
    amount = float(match.group(1))
    unit = match.group(2)
    return amount * font_size if unit == "em" else amount


def _kerning_subtables(font: Any) -> list[tuple[Any, dict[str, int]]]:
    """Extract Format 1/2 GPOS 'kern' lookup subtables."""
    if "GPOS" not in font:
        return []
    table = font["GPOS"].table
    if not table.FeatureList or not table.LookupList:
        return []
    indices = sorted({
        idx
        for rec in table.FeatureList.FeatureRecord
        if rec.FeatureTag == "kern"
        for idx in rec.Feature.LookupListIndex
    })
    out: list[tuple[Any, dict[str, int]]] = []
    for idx in indices:
        lookup = table.LookupList.Lookup[idx]
        for sub in lookup.SubTable:
            kind = lookup.LookupType
            if kind == 9:
                kind, sub = sub.ExtensionLookupType, sub.ExtSubTable
            if kind == 2 and sub.Coverage is not None:
                out.append((sub, {glyph: pos for pos, glyph in enumerate(sub.Coverage.glyphs)}))
    return out


def _x_advance(value: Any) -> int:
    return (getattr(value, "XAdvance", 0) or 0) if value is not None else 0


def _pair_value(subtables: list[tuple[Any, dict[str, int]]], left: str, right: str) -> int:
    for sub, coverage in subtables:
        cov_idx = coverage.get(left)
        if cov_idx is None:
            continue
        if sub.Format == 1:
            for rec in sub.PairSet[cov_idx].PairValueRecord:
                if rec.SecondGlyph == right:
                    return _x_advance(rec.Value1)
        elif sub.Format == 2:
            c1 = sub.ClassDef1.classDefs.get(left, 0)
            c2 = sub.ClassDef2.classDefs.get(right, 0)
            return _x_advance(sub.Class1Record[c1].Class2Record[c2].Value1)
    return 0


class Face:
    """Loaded OpenType/TrueType font face with cached glyph metrics and GPOS kerning."""

    def __init__(self, path: Path, label: str) -> None:
        if not HAS_FONTTOOLS or TTFont is None:
            raise RuntimeError("fontTools is required to load font faces")
        font = TTFont(str(path))
        self.path = path
        self.label = label
        self.glyphs = font.getGlyphSet()
        self.cmap: dict[int, str] = font.getBestCmap() or {}
        self.hmtx = font["hmtx"]
        self.upm: int = int(font["head"].unitsPerEm)
        os2 = font["OS/2"]
        self.ascent: int = int(os2.sTypoAscender)
        self.descent: int = int(os2.sTypoDescender)
        self.cap: int = int(os2.sCapHeight)
        self.x_height: int = int(getattr(os2, "sxHeight", 0) or round(self.cap * 0.72))
        self._pairs = _kerning_subtables(font)
        self._kern_cache: dict[tuple[str, str], int] = {}

    def glyph_for_char(self, char: str) -> str | None:
        """Map one character to a glyph name, or None if unmapped."""
        code = ord(char)
        if code in self.cmap:
            return self.cmap[code]
        if char == "\u00a0" and 0x20 in self.cmap:
            return self.cmap[0x20]
        return None

    def can_render(self, text: str) -> bool:
        """True when every non-combining character in ``text`` resolves in this face."""
        for char in text:
            if unicodedata.category(char) in {"Mn", "Me"}:
                if ord(char) not in self.cmap:
                    return False
                continue
            lookup = " " if char.isspace() else char
            if self.glyph_for_char(lookup) is None:
                return False
        return True

    def kern(self, left: str, right: str) -> int:
        """Return GPOS pair kerning adjustment in font units."""
        key = (left, right)
        cached = self._kern_cache.get(key)
        if cached is None:
            cached = _pair_value(self._pairs, left, right)
            self._kern_cache[key] = cached
        return cached

    def positions(
        self,
        text: str,
        size: float,
        letter_spacing: float = 0.0,
        *,
        trailing_tracking: bool = False,
    ) -> tuple[list[tuple[str, float]], float] | None:
        """Return ``([(glyph_name, x_px), ...], total_width_px)`` or None if unmapped."""
        glyph_names: list[str] = []
        for char in text:
            if unicodedata.category(char) in {"Mn", "Me"}:
                if ord(char) not in self.cmap:
                    return None
                continue
            gname = self.glyph_for_char(" " if char.isspace() else char)
            if gname is None:
                return None
            glyph_names.append(gname)

        scale = size / self.upm
        placed: list[tuple[str, float]] = []
        units = 0
        for idx, gname in enumerate(glyph_names):
            if idx:
                units += self.kern(glyph_names[idx - 1], gname)
            x_px = units * scale + idx * letter_spacing
            placed.append((gname, x_px))
            units += self.hmtx[gname][0]

        n_glyphs = len(glyph_names)
        gaps = n_glyphs if trailing_tracking else max(0, n_glyphs - 1)
        total_px = units * scale + gaps * letter_spacing
        return placed, total_px

    def measure(
        self,
        text: str,
        size: float,
        letter_spacing: float = 0.0,
        *,
        trailing_tracking: bool = False,
    ) -> float | None:
        """Return exact width in px when every glyph is mapped, else None."""
        pos = self.positions(
            text, size, letter_spacing, trailing_tracking=trailing_tracking
        )
        return None if pos is None else pos[1]

    def measure_mixed(
        self,
        text: str,
        size: float,
        letter_spacing: float = 0.0,
        *,
        fallback_narrow_em: float = 0.60,
        fallback_wide_em: float = 1.0,
    ) -> tuple[float, bool]:
        """Measure ``text`` using exact glyphs where mapped and budget advances elsewhere."""
        scale = size / self.upm
        total_px = 0.0
        prev_glyph: str | None = None
        glyph_count = 0
        all_mapped = True
        for char in text:
            if unicodedata.category(char) in {"Mn", "Me"}:
                continue
            glyph_count += 1
            gname = self.glyph_for_char(" " if char.isspace() else char)
            if gname is None:
                all_mapped = False
                prev_glyph = None
                wide = unicodedata.east_asian_width(char) in {"W", "F"}
                total_px += (fallback_wide_em if wide else fallback_narrow_em) * size
                continue
            if prev_glyph is not None:
                total_px += self.kern(prev_glyph, gname) * scale
            total_px += self.hmtx[gname][0] * scale
            prev_glyph = gname
        total_px += max(0, glyph_count - 1) * letter_spacing
        return total_px, all_mapped

    def baseline_shift(self, size: float, dominant_baseline: str = "auto") -> float:
        """Return downward shift from authored ``y`` to the alphabetic baseline in px."""
        mode = (dominant_baseline or "auto").strip().casefold()
        scale = size / self.upm
        if mode in {"", "auto", "alphabetic", "baseline", "inherit"}:
            return 0.0
        if mode == "middle":
            return (self.x_height / 2.0) * scale
        if mode == "central":
            return ((self.ascent + self.descent) / 2.0) * scale
        if mode in {"hanging", "text-before-edge", "text-top"}:
            return self.ascent * scale
        if mode == "mathematical":
            return (self.cap * 0.5) * scale
        if mode in {"ideographic", "text-after-edge", "text-bottom"}:
            return self.descent * scale
        return 0.0

    def outline(
        self,
        text: str,
        size: float,
        x: float,
        baseline: float,
        letter_spacing: float = 0.0,
    ) -> str | None:
        """Render ``text`` as an SVG path ``d`` string at ``(x, baseline)``."""
        if not HAS_FONTTOOLS or SVGPathPen is None or TransformPen is None:
            return None
        pos = self.positions(text, size, letter_spacing, trailing_tracking=False)
        if pos is None:
            return None
        placed, _width = pos
        pen = SVGPathPen(self.glyphs, ntos=fmt_svg_num)
        scale = size / self.upm
        for gname, gx in placed:
            self.glyphs[gname].draw(
                TransformPen(pen, (scale, 0, 0, -scale, x + gx, baseline))
            )
        return pen.getCommands()


def resolve_face(
    font_family: str | None = None,
    weight: str | int | None = None,
    style: str | None = None,
) -> Face | None:
    """Resolve the closest bundled static font face, or None if unavailable."""
    if not HAS_FONTTOOLS:
        return None
    kind = classify_family(font_family)
    w_num = parse_weight(weight)
    if kind == "mono":
        chosen_w = min(_MONO_FILES, key=lambda cand: (abs(cand - w_num), -cand))
        filename, label = _MONO_FILES[chosen_w]
    elif kind == "serif":
        filename, label = _SERIF_FILES[parse_italic(style)]
    else:
        chosen_w = min(_SANS_FILES, key=lambda cand: (abs(cand - w_num), -cand))
        filename, label = _SANS_FILES[chosen_w]

    path = _find_font_file(filename)
    if path is None:
        return None
    key = (path.resolve(), label)
    cached = _FACE_CACHE.get(key)
    if cached is None:
        cached = Face(path, label)
        _FACE_CACHE[key] = cached
    return cached
