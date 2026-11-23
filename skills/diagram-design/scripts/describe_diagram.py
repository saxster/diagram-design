#!/usr/bin/env python3
"""Extract a plain-text semantic summary from a diagram HTML or standalone SVG.

Reads visual structure (title, description, zones, boxed nodes with sublabels
and badges, arrowed connections with labels, focal highlights, and motion
steps) from both standard ``<text>`` diagrams and ``--outline-text`` SVGs.
Also provides placeholder-leak and expected-term checks for ``self_check.py``.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Sequence

SVG_NUM_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
TRANSLATE_RE = re.compile(
    rf"^\s*translate\(\s*({SVG_NUM_RE.pattern})(?:[\s,]+({SVG_NUM_RE.pattern}))?\s*\)\s*$"
)
FONT_SIZE_RE = re.compile(rf"({SVG_NUM_RE.pattern})\s*(?:px)?")
PLACEHOLDER_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:TODO|TBD|FIXME|PLACEHOLDER|Lorem\s+ipsum)(?![A-Za-z0-9_])"
    r"|\{\{[^{}\n]{1,64}\}\}",
    re.IGNORECASE,
)
SKIP_CONTAINERS = frozenset({"defs", "symbol", "marker", "pattern", "clippath", "mask"})
ACCENT_HEXES = ("#bf4520", "#f08a59", "#ff5a36", "191,69,32", "240,138,89", "255,90,54")
LINK_HEXES = ("#2e5aa8", "#739fdf", "46,90,168", "115,159,223")


def _load_verify_geometry():
    key = "_diagram_design_verify_geometry"
    if key in sys.modules:
        return sys.modules[key]
    target = Path(__file__).resolve().parent / "verify_geometry.py"
    if not target.is_file():
        return None
    spec = importlib.util.spec_from_file_location(key, target)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


def _parse_num(value: str | None, default: float | None = None) -> float | None:
    if value is None:
        return default
    cleaned = value.strip()
    if re.fullmatch(SVG_NUM_RE.pattern, cleaned):
        return float(cleaned)
    return default


def _parse_style(style_attr: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for decl in style_attr.split(";"):
        name, sep, val = decl.partition(":")
        if sep:
            out[name.strip().casefold()] = val.strip()
    return out


def _is_accent(text: str) -> bool:
    low = text.replace(" ", "").casefold()
    return "focal" in low or "accent" in low or any(tok in low for tok in ACCENT_HEXES)


def _is_link(text: str) -> bool:
    low = text.replace(" ", "").casefold()
    return "arrow-link" in low or any(tok in low for tok in LINK_HEXES)


def _is_faint_watermark(text_item: "_TextItem") -> bool:
    if text_item.font_size is not None and text_item.font_size >= 22:
        low = text_item.fill.replace(" ", "").casefold()
        if "rgba(" in low and re.search(r",0?\.[01]\d?\)", low):
            return True
        if text_item.fill_opacity is not None and text_item.fill_opacity <= 0.25:
            return True
    return False


@dataclass
class _RectItem:
    x: float
    y: float
    w: float
    h: float
    cls: str
    fill: str
    stroke: str
    focal: bool
    order: int

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    @property
    def area(self) -> float:
        return self.w * self.h


@dataclass
class _TextItem:
    text: str
    x: float
    y: float
    font_size: float | None
    weight: int
    mono: bool
    cls: str
    fill: str
    focal: bool
    order: int
    fill_opacity: float | None = None


@dataclass
class _ConnectorItem:
    points: list[tuple[float, float]]
    dashed: bool
    kind: str
    order: int


@dataclass
class ZoneSummary:
    label: str
    x: float
    y: float
    w: float
    h: float
    node_names: list[str] = field(default_factory=list)


@dataclass
class NodeSummary:
    label: str
    sublabels: list[str] = field(default_factory=list)
    badges: list[str] = field(default_factory=list)
    focal: bool = False
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    zone: str | None = None


@dataclass
class ConnectionSummary:
    source: str
    target: str
    label: str | None = None
    kind: str = "flow"
    dashed: bool = False


@dataclass
class DiagramSummary:
    title: str = ""
    desc: str = ""
    eyebrow: str = ""
    heading: str = ""
    viewbox: tuple[float, float, float, float] | None = None
    zones: list[ZoneSummary] = field(default_factory=list)
    nodes: list[NodeSummary] = field(default_factory=list)
    connections: list[ConnectionSummary] = field(default_factory=list)
    other_labels: list[str] = field(default_factory=list)
    motion_steps: list[tuple[int, str]] = field(default_factory=list)
    all_text: list[str] = field(default_factory=list)
    duplicate_node_labels: list[str] = field(default_factory=list)


class _SvgSemanticParser(HTMLParser):
    """Collect geometric primitives and labels from the first visible SVG."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.eyebrow = ""
        self.heading = ""
        self._capture_html: str | None = None
        self._html_buf: list[str] = []

        self._svg_depth = 0
        self._done_svg = False
        self._skip_depth = 0
        self._order = 0

        self.title = ""
        self.desc = ""
        self.viewbox: tuple[float, float, float, float] | None = None
        self.rects: list[_RectItem] = []
        self.texts: list[_TextItem] = []
        self.connectors: list[_ConnectorItem] = []
        self.lifelines: list[tuple[float, float, float]] = []  # (x, y_top, y_bottom)
        self.motion_steps: list[tuple[int, str]] = []

        self._frames: list[tuple[str, tuple[float, float], dict[str, str]]] = []
        self._in_title_desc: str | None = None
        self._td_buf: list[str] = []
        self._active_text: dict[str, Any] | None = None
        self._active_outlined: dict[str, Any] | None = None

    def _cur_offset(self) -> tuple[float, float]:
        return self._frames[-1][1] if self._frames else (0.0, 0.0)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        data = {k.casefold(): (v or "") for k, v in attrs}

        if self._svg_depth == 0:
            if tag == "p" and "eyebrow" in data.get("class", "").split():
                self._capture_html = "eyebrow"
                self._html_buf = []
                return
            if tag == "h1" and not self.heading:
                self._capture_html = "h1"
                self._html_buf = []
                return
            if tag == "svg" and not self._done_svg and data.get("aria-hidden", "").casefold() != "true":
                self._svg_depth = 1
                nums = [float(x) for x in SVG_NUM_RE.findall(data.get("viewbox", ""))]
                if len(nums) == 4 and nums[2] > 0 and nums[3] > 0:
                    self.viewbox = (nums[0], nums[1], nums[2], nums[3])
                self._frames.append((tag, (0.0, 0.0), data))
            return

        self._svg_depth += 1
        if self._skip_depth > 0:
            self._skip_depth += 1
            return

        style = _parse_style(data.get("style", ""))
        hidden = (
            tag in SKIP_CONTAINERS
            or data.get("display", style.get("display", "")).casefold() == "none"
            or data.get("visibility", style.get("visibility", "")).casefold() in {"hidden", "collapse"}
            or data.get("opacity", style.get("opacity", "")) in {"0", "0.0"}
        )
        if hidden:
            self._skip_depth = 1
            return

        parent_off = self._cur_offset()
        t_attr = data.get("transform", "")
        t_match = TRANSLATE_RE.match(t_attr) if t_attr else None
        offset = (
            (parent_off[0] + float(t_match.group(1)), parent_off[1] + float(t_match.group(2) or 0.0))
            if t_match
            else parent_off
        )
        self._frames.append((tag, offset, data))

        if "data-motion-item" in data and "data-motion-decorative" not in data:
            step = _parse_num(data.get("data-step"))
            label = data.get("aria-label", "").strip()
            if step is not None and label:
                self.motion_steps.append((int(step), label))

        if self._svg_depth == 2 and tag in {"title", "desc"}:
            self._in_title_desc = tag
            self._td_buf = []
            return

        if data.get("data-outlined-text", "").casefold() == "true" and self._active_outlined is None:
            pos_str = data.get("data-text-pos", "")
            pos_nums = [float(v) for v in SVG_NUM_RE.findall(pos_str)]
            ox = (pos_nums[0] + offset[0]) if len(pos_nums) >= 2 else None
            oy = (pos_nums[1] + offset[1]) if len(pos_nums) >= 2 else None
            meta = data.get("data-text-meta", "").split(";")
            m_size = _parse_num(meta[0]) if len(meta) >= 1 and meta[0] else None
            m_weight = int(float(meta[1])) if len(meta) >= 2 and meta[1] else 400
            m_mono = (meta[2] == "mono") if len(meta) >= 3 else False
            self._active_outlined = {
                "depth": self._svg_depth,
                "x": ox,
                "y": oy,
                "font_size": m_size,
                "weight": m_weight,
                "mono": m_mono,
                "cls": data.get("class", ""),
                "fill": data.get("fill", style.get("fill", "")),
                "fill_opacity": _parse_num(data.get("opacity", style.get("opacity"))),
                "focal": _is_accent(data.get("class", "") + " " + data.get("fill", "")),
                "title": [],
                "in_title": False,
                "order": self._order,
            }
            self._order += 1
            return

        if self._active_outlined is not None:
            if tag == "title":
                self._active_outlined["in_title"] = True
            elif tag == "path":
                pfill = data.get("fill", "")
                if _is_accent(pfill):
                    self._active_outlined["focal"] = True
                if pfill and not self._active_outlined["fill"]:
                    self._active_outlined["fill"] = pfill
                p_op = _parse_num(data.get("fill-opacity"))
                if p_op is not None and self._active_outlined["fill_opacity"] is None:
                    self._active_outlined["fill_opacity"] = p_op
            return

        if tag == "rect":
            self._record_rect(data, style, offset)
        elif tag == "line":
            self._record_line(data, style, offset)
        elif tag == "path":
            self._record_path(data, style, offset)
        elif tag == "text":
            self._start_text(data, style, offset)
        elif tag == "tspan" and self._active_text is not None:
            if ("x" in data or "y" in data) and self._active_text["parts"]:
                last = self._active_text["parts"][-1]
                if not last.endswith((" ", "\n")):
                    self._active_text["parts"].append(" ")

    def _record_rect(self, data: dict[str, str], style: dict[str, str], offset: tuple[float, float]) -> None:
        x = _parse_num(data.get("x"), 0.0)
        y = _parse_num(data.get("y"), 0.0)
        w = _parse_num(data.get("width"))
        h = _parse_num(data.get("height"))
        if x is None or y is None or w is None or h is None or w <= 0 or h <= 0:
            return
        fill = data.get("fill", style.get("fill", ""))
        stroke = data.get("stroke", style.get("stroke", ""))
        cls = data.get("class", "")
        if not cls and fill.casefold() in {"", "none", "transparent"} and stroke.casefold() in {"", "none", "transparent"}:
            return
        focal = _is_accent(f"{cls} {stroke} {fill}")
        self.rects.append(
            _RectItem(x + offset[0], y + offset[1], w, h, cls, fill, stroke, focal, self._order)
        )
        self._order += 1

    def _record_line(self, data: dict[str, str], style: dict[str, str], offset: tuple[float, float]) -> None:
        cls = data.get("class", "")
        has_marker = any(k in data for k in ("marker-end", "marker-start"))
        x1 = _parse_num(data.get("x1"), 0.0)
        y1 = _parse_num(data.get("y1"), 0.0)
        x2 = _parse_num(data.get("x2"), 0.0)
        y2 = _parse_num(data.get("y2"), 0.0)
        if None in (x1, y1, x2, y2):
            return
        assert x1 is not None and y1 is not None and x2 is not None and y2 is not None
        p1 = (x1 + offset[0], y1 + offset[1])
        p2 = (x2 + offset[0], y2 + offset[1])
        if not has_marker and not any(tok in cls.casefold() for tok in ("connector", "edge", "spoke", "arrow")):
            if abs(p1[0] - p2[0]) <= 1.0 and abs(p2[1] - p1[1]) >= 120.0:
                self.lifelines.append((p1[0], min(p1[1], p2[1]), max(p1[1], p2[1])))
            return
        self._push_connector([p1, p2], data, style)

    def _record_path(self, data: dict[str, str], style: dict[str, str], offset: tuple[float, float]) -> None:
        cls = data.get("class", "")
        has_marker = any(k in data for k in ("marker-end", "marker-start"))
        if not has_marker and not any(tok in cls.casefold() for tok in ("ring", "spoke", "connector", "edge")):
            return
        d = data.get("d", "")
        vg = _load_verify_geometry()
        pts: list[tuple[float, float]] = []
        if vg is not None and d:
            segs = vg.path_segments(d)
            if segs:
                pts.append((segs[0][1] + offset[0], segs[0][2] + offset[1]))
                for _, _, _, x2, y2 in segs:
                    pts.append((x2 + offset[0], y2 + offset[1]))
        if not pts and d:
            nums = [float(n) for n in SVG_NUM_RE.findall(d)]
            if len(nums) >= 4:
                pts = [(nums[0] + offset[0], nums[1] + offset[1]), (nums[-2] + offset[0], nums[-1] + offset[1])]
        if len(pts) >= 2:
            self._push_connector(pts, data, style)

    def _push_connector(self, pts: list[tuple[float, float]], data: dict[str, str], style: dict[str, str]) -> None:
        cls = data.get("class", "")
        stroke = data.get("stroke", style.get("stroke", ""))
        marker = data.get("marker-end", "") + " " + data.get("marker-start", "")
        dashed = bool(data.get("stroke-dasharray") or style.get("stroke-dasharray")) or "spoke" in cls.casefold()
        sig = f"{cls} {stroke} {marker}"
        kind = "focal" if _is_accent(sig) else "link" if _is_link(sig) else "flow"
        self.connectors.append(_ConnectorItem(pts, dashed, kind, self._order))
        self._order += 1

    def _start_text(self, data: dict[str, str], style: dict[str, str], offset: tuple[float, float]) -> None:
        x = _parse_num(data.get("x"))
        y = _parse_num(data.get("y"))
        if x is None or y is None:
            return
        cls = data.get("class", "")
        fill = data.get("fill", style.get("fill", ""))
        fam = data.get("font-family", style.get("font-family", ""))
        fsize_raw = data.get("font-size", style.get("font-size", ""))
        fmatch = FONT_SIZE_RE.search(fsize_raw)
        fsize = float(fmatch.group(1)) if fmatch else None
        weight_raw = data.get("font-weight", style.get("font-weight", ""))
        weight = 600 if ("semibold" in weight_raw or "600" in weight_raw or "bold" in weight_raw or "700" in weight_raw or "node-name" in cls or "hub-name" in cls) else 400
        mono = (
            "mono" in fam.casefold()
            or "sublabel" in cls.casefold()
            or "arrow-label" in cls.casefold()
            or cls.casefold().endswith("-sub")
        )
        self._active_text = {
            "x": x + offset[0],
            "y": y + offset[1],
            "font_size": fsize,
            "weight": weight,
            "mono": mono,
            "cls": cls,
            "fill": fill,
            "focal": _is_accent(f"{cls} {fill}"),
            "parts": [],
            "order": self._order,
        }
        self._order += 1

    def handle_data(self, data: str) -> None:
        if self._capture_html is not None:
            self._html_buf.append(data)
            return
        if self._in_title_desc is not None:
            self._td_buf.append(data)
            return
        if self._active_outlined is not None and self._active_outlined["in_title"]:
            self._active_outlined["title"].append(data)
            return
        if self._active_text is not None:
            self._active_text["parts"].append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self._capture_html == "eyebrow" and tag == "p":
            self.eyebrow = re.sub(r"\s+", " ", "".join(self._html_buf)).strip()
            self._capture_html = None
            return
        if self._capture_html == "h1" and tag == "h1":
            self.heading = re.sub(r"\s+", " ", "".join(self._html_buf)).strip()
            self._capture_html = None
            return

        if self._svg_depth == 0:
            return

        if self._skip_depth > 0:
            self._skip_depth -= 1
            self._svg_depth -= 1
            return

        if self._in_title_desc == tag:
            text = re.sub(r"\s+", " ", "".join(self._td_buf)).strip()
            if tag == "title" and not self.title:
                self.title = text
            elif tag == "desc" and not self.desc:
                self.desc = text
            self._in_title_desc = None
            self._td_buf = []

        if self._active_outlined is not None:
            if tag == "title":
                self._active_outlined["in_title"] = False
            if self._svg_depth == self._active_outlined["depth"]:
                title_text = re.sub(r"\s+", " ", "".join(self._active_outlined["title"])).strip()
                if title_text and self._active_outlined["x"] is not None and self._active_outlined["y"] is not None:
                    cls = self._active_outlined["cls"]
                    mono = self._active_outlined["mono"] or (
                        "sublabel" in cls or "arrow-label" in cls or cls.casefold().endswith("-sub")
                    )
                    weight = self._active_outlined["weight"]
                    if "node-name" in cls or "hub-name" in cls:
                        weight = max(weight, 600)
                    self.texts.append(
                        _TextItem(
                            title_text,
                            self._active_outlined["x"],
                            self._active_outlined["y"],
                            self._active_outlined["font_size"],
                            weight,
                            mono,
                            cls,
                            self._active_outlined["fill"],
                            self._active_outlined["focal"],
                            self._active_outlined["order"],
                            self._active_outlined["fill_opacity"],
                        )
                    )
                self._active_outlined = None

        if tag == "text" and self._active_text is not None:
            txt = re.sub(r"\s+", " ", "".join(self._active_text["parts"])).strip()
            if txt:
                self.texts.append(
                    _TextItem(
                        txt,
                        self._active_text["x"],
                        self._active_text["y"],
                        self._active_text["font_size"],
                        self._active_text["weight"],
                        self._active_text["mono"],
                        self._active_text["cls"],
                        self._active_text["fill"],
                        self._active_text["focal"],
                        self._active_text["order"],
                    )
                )
            self._active_text = None

        if self._frames:
            self._frames.pop()
        self._svg_depth -= 1
        if self._svg_depth == 0:
            self._done_svg = True


def _point_to_rect_distance(px: float, py: float, r: _RectItem) -> float:
    dx = max(r.x - px, 0.0, px - r.right)
    dy = max(r.y - py, 0.0, py - r.bottom)
    return math.hypot(dx, dy)


def _point_to_polyline_distance(px: float, py: float, pts: list[tuple[float, float]]) -> float:
    best = math.inf
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        vx, vy = x2 - x1, y2 - y1
        seg_len_sq = vx * vx + vy * vy
        if seg_len_sq <= 1e-9:
            d = math.hypot(px - x1, py - y1)
        else:
            t = max(0.0, min(1.0, ((px - x1) * vx + (py - y1) * vy) / seg_len_sq))
            d = math.hypot(px - (x1 + t * vx), py - (y1 + t * vy))
        if d < best:
            best = d
    return best


def _dedupe_rects(rects: list[_RectItem], viewbox: tuple[float, float, float, float] | None) -> list[_RectItem]:
    vb_area = viewbox[2] * viewbox[3] if viewbox else None
    merged: list[_RectItem] = []
    for r in rects:
        if vb_area is not None and r.area >= 0.85 * vb_area:
            continue
        is_box_class = any(k in r.cls.casefold() for k in ("station", "hub", "node", "zone", "box", "card", "lane"))
        if not is_box_class and (r.w < 44 or r.h < 28):
            continue
        matched = None
        for existing in merged:
            if (
                abs(existing.x - r.x) <= 2.5
                and abs(existing.y - r.y) <= 2.5
                and abs(existing.w - r.w) <= 2.5
                and abs(existing.h - r.h) <= 2.5
            ):
                matched = existing
                break
        if matched is not None:
            matched.focal = matched.focal or r.focal
            if r.cls and r.cls not in matched.cls:
                matched.cls = f"{matched.cls} {r.cls}".strip()
            if r.stroke and r.stroke not in {"none", "transparent"}:
                matched.stroke = r.stroke
            continue
        merged.append(r)
    return merged


def _contains_rect(outer: _RectItem, inner: _RectItem) -> bool:
    return (
        outer is not inner
        and outer.area >= 1.5 * inner.area
        and outer.x - 2 <= inner.x
        and outer.y - 2 <= inner.y
        and outer.right + 2 >= inner.right
        and outer.bottom + 2 >= inner.bottom
    )


def describe_document(source: str) -> DiagramSummary:
    """Parse an HTML diagram or standalone SVG and summarize its semantic structure."""
    parser = _SvgSemanticParser()
    parser.feed(source)
    parser.close()

    summary = DiagramSummary(
        title=parser.title,
        desc=parser.desc,
        eyebrow=parser.eyebrow,
        heading=parser.heading,
        viewbox=parser.viewbox,
        motion_steps=sorted(parser.motion_steps, key=lambda item: (item[0], item[1])),
        all_text=[t.text for t in parser.texts],
    )

    candidate_rects = _dedupe_rects(parser.rects, parser.viewbox)
    zone_rects: list[_RectItem] = []
    node_rects: list[_RectItem] = []
    for rect in candidate_rects:
        has_children = any(_contains_rect(rect, other) for other in candidate_rects)
        is_fragment = rect.w >= 320 and rect.h >= 160 and "hub" not in rect.cls.casefold()
        if (has_children or is_fragment) and rect.w >= 80 and rect.h >= 60:
            zone_rects.append(rect)
        else:
            node_rects.append(rect)

    claimed_texts: set[int] = set()
    node_map: dict[int, NodeSummary] = {}

    # Sort nodes reading-order (row bands of 24px, then left-to-right).
    ordered_nodes = sorted(node_rects, key=lambda r: (round(r.y / 24.0), r.x, r.y))
    for rect in ordered_nodes:
        inside = [
            t for t in parser.texts
            if t.order not in claimed_texts
            and rect.x - 2 <= t.x <= rect.right + 2
            and rect.y - 2 <= t.y <= rect.bottom + 2
            and not _is_faint_watermark(t)
        ]
        # Skip degenerate empty containers unless they carry focal styling.
        if not inside and not rect.cls:
            continue
        badges: list[str] = []
        primaries: list[str] = []
        sublabels: list[str] = []
        focal = rect.focal
        for t in inside:
            claimed_texts.add(t.order)
            if t.focal:
                focal = True
            is_badge = (
                (t.font_size is not None and t.font_size <= 7.5 and len(t.text) <= 12)
                or (t.mono and t.text.isupper() and len(t.text) <= 6 and t.y <= rect.y + 22)
            )
            if is_badge:
                badges.append(t.text)
            elif "node-name" in t.cls or "hub-name" in t.cls or t.weight >= 600 or (not t.mono and (t.font_size or 12) >= 10):
                primaries.append(t.text)
            else:
                sublabels.append(t.text)
        if not primaries and sublabels:
            primaries.append(sublabels.pop(0))
        elif not primaries and badges:
            primaries.append(badges.pop(0))
        label = " ".join(primaries) if primaries else (rect.cls or f"Box({rect.x:g},{rect.y:g})")
        node = NodeSummary(
            label=label,
            sublabels=sublabels,
            badges=badges,
            focal=focal,
            x=rect.x,
            y=rect.y,
            w=rect.w,
            h=rect.h,
        )
        node_map[rect.order] = node
        summary.nodes.append(node)

    # Identify zones using header text in the top band (so connector labels
    # crossing inside a zone remain available for connectors).
    for zrect in sorted(zone_rects, key=lambda r: (round(r.y / 24.0), r.x)):
        header_limit_y = zrect.y + min(36.0, zrect.h * 0.35)
        ztexts = [
            t for t in parser.texts
            if t.order not in claimed_texts
            and zrect.x - 2 <= t.x <= zrect.right + 2
            and zrect.y - 2 <= t.y <= header_limit_y
        ]
        for t in ztexts:
            claimed_texts.add(t.order)
        zlabel = " / ".join(t.text for t in ztexts) if ztexts else (zrect.cls or f"Zone({zrect.x:g},{zrect.y:g})")
        child_names: list[str] = []
        for nrect in ordered_nodes:
            if nrect.order in node_map and _contains_rect(zrect, nrect):
                node = node_map[nrect.order]
                if node.zone is None:
                    node.zone = zlabel
                child_names.append(node.label)
        summary.zones.append(ZoneSummary(zlabel, zrect.x, zrect.y, zrect.w, zrect.h, child_names))

    # Associate connectors with endpoints and nearest unclaimed arrow labels.
    free_texts = [t for t in parser.texts if t.order not in claimed_texts and not _is_faint_watermark(t)]

    # Map sequence lifelines (vertical lines attached to an actor node) back to their node.
    lifeline_nodes: list[tuple[float, float, float, NodeSummary]] = []
    for lx, ly_top, ly_bot in parser.lifelines:
        for nrect in ordered_nodes:
            if nrect.order not in node_map:
                continue
            if nrect.x - 2 <= lx <= nrect.right + 2 and abs(ly_top - nrect.bottom) <= 12:
                lifeline_nodes.append((lx, ly_top, ly_bot, node_map[nrect.order]))
                break

    def nearest_node(pt: tuple[float, float]) -> tuple[NodeSummary | None, float]:
        best_node: NodeSummary | None = None
        best_dist = math.inf
        for nrect in ordered_nodes:
            if nrect.order not in node_map:
                continue
            d = _point_to_rect_distance(pt[0], pt[1], nrect)
            if d < best_dist:
                best_dist = d
                best_node = node_map[nrect.order]
        for lx, ly_top, ly_bot, lnode in lifeline_nodes:
            dy = max(ly_top - pt[1], 0.0, pt[1] - ly_bot)
            d = math.hypot(pt[0] - lx, dy)
            if d < best_dist:
                best_dist = d
                best_node = lnode
        return best_node, best_dist

    for conn in parser.connectors:
        src_node, d_src = nearest_node(conn.points[0])
        dst_node, d_dst = nearest_node(conn.points[-1])
        if d_src > 22 and d_dst > 22:
            continue
        src_name = src_node.label if (src_node is not None and d_src <= 22) else f"({conn.points[0][0]:g},{conn.points[0][1]:g})"
        dst_name = dst_node.label if (dst_node is not None and d_dst <= 22) else f"({conn.points[-1][0]:g},{conn.points[-1][1]:g})"

        best_lbl: _TextItem | None = None
        best_lbl_dist = 42.0
        for t in free_texts:
            d = _point_to_polyline_distance(t.x, t.y, conn.points)
            if d < best_lbl_dist:
                best_lbl_dist = d
                best_lbl = t
        label_str = None
        if best_lbl is not None:
            label_str = best_lbl.text
            claimed_texts.add(best_lbl.order)
            free_texts = [t for t in free_texts if t.order != best_lbl.order]
        summary.connections.append(
            ConnectionSummary(src_name, dst_name, label_str, conn.kind, conn.dashed)
        )

    summary.other_labels = [t.text for t in free_texts]
    counts = Counter(n.label for n in summary.nodes if n.label)
    summary.duplicate_node_labels = sorted(label for label, n in counts.items() if n > 1)
    return summary


def format_summary(summary: DiagramSummary) -> str:
    """Format a DiagramSummary as a concise plain-text semantic readout."""
    lines: list[str] = []
    heading = summary.title or summary.heading or "(untitled diagram)"
    lines.append(f"Title: {heading}")
    if summary.eyebrow:
        lines.append(f"Category: {summary.eyebrow}")
    if summary.desc:
        lines.append(f"Description: {summary.desc}")
    if summary.viewbox:
        _, _, w, h = summary.viewbox
        lines.append(f"Canvas: {w:g}x{h:g}")

    if summary.zones:
        lines.append("Zones:")
        for z in summary.zones:
            members = ", ".join(z.node_names) if z.node_names else "(empty)"
            lines.append(f"- {z.label}: {members}")

    if summary.nodes:
        lines.append(f"Nodes ({len(summary.nodes)}):")
        for n in summary.nodes:
            tags: list[str] = []
            if n.focal:
                tags.append("focal")
            if n.badges:
                tags.extend(n.badges)
            if n.zone:
                tags.append(f"in {n.zone}")
            tag_str = f" [{', '.join(tags)}]" if tags else ""
            sub_str = f" — {'; '.join(n.sublabels)}" if n.sublabels else ""
            lines.append(f"- {n.label}{tag_str}{sub_str}")

    if summary.connections:
        lines.append(f"Connections ({len(summary.connections)}):")
        for c in summary.connections:
            traits = [t for t in (c.kind if c.kind != "flow" else "", "dashed" if c.dashed else "") if t]
            trait_str = f" ({', '.join(traits)})" if traits else ""
            lbl_str = f" [{c.label}]" if c.label else ""
            lines.append(f"- {c.source} -> {c.target}{lbl_str}{trait_str}")

    if summary.motion_steps:
        steps_str = "; ".join(f"{n}. {lbl}" for n, lbl in summary.motion_steps)
        lines.append(f"Motion steps: {steps_str}")

    if summary.duplicate_node_labels:
        lines.append(f"Note: duplicate node labels: {', '.join(summary.duplicate_node_labels)}")
    return "\n".join(lines)


def summary_to_dict(summary: DiagramSummary) -> dict[str, Any]:
    """Convert DiagramSummary into a JSON-serializable dict."""
    return asdict(summary)


def check_placeholders(source: str, errors: list[str]) -> None:
    """Flag placeholder tokens left inside visible SVG labels, title, or desc."""
    parser = _SvgSemanticParser()
    parser.feed(source)
    parser.close()
    candidates: list[tuple[str, str]] = []
    if parser.title:
        candidates.append(("svg <title>", parser.title))
    if parser.desc:
        candidates.append(("svg <desc>", parser.desc))
    for t in parser.texts:
        candidates.append(("svg text", t.text))
    for where, text in candidates:
        match = PLACEHOLDER_RE.search(text)
        if match:
            errors.append(
                f"placeholder text {match.group(0)!r} left in {where} ({text!r}); "
                "replace it with real domain wording"
            )


def check_expected_terms(
    summary: DiagramSummary, expect: Sequence[str], errors: list[str]
) -> None:
    """Fail if any caller-supplied required concept/term is absent from the diagram."""
    if not expect:
        return
    haystack_parts = [summary.title, summary.desc, summary.eyebrow, summary.heading]
    haystack_parts.extend(summary.all_text)
    haystack_parts.extend(lbl for _, lbl in summary.motion_steps)
    haystack = "\n".join(part for part in haystack_parts if part).casefold()
    for term in expect:
        needle = term.strip()
        if needle and needle.casefold() not in haystack:
            errors.append(
                f"expected diagram term {needle!r} was not found in SVG text, title, or desc"
            )


def check_semantics(
    summary: DiagramSummary,
    expect: Sequence[str] = (),
    *,
    strict: bool = False,
) -> tuple[list[str], list[str]]:
    """Return ``(errors, warnings)`` from semantic hygiene checks on a parsed diagram."""
    errors: list[str] = []
    warnings: list[str] = []
    if not summary.title.strip():
        errors.append("diagram <title> is missing or empty")
    if not summary.desc.strip():
        errors.append("diagram <desc> is missing or empty")
    elif summary.title.strip() and summary.desc.strip().casefold() == summary.title.strip().casefold():
        errors.append("diagram <desc> duplicates <title>; summarize topology or data flow instead")
    elif len(summary.desc.strip()) < 12:
        warnings.append(f"diagram <desc> is very short ({len(summary.desc.strip())} chars)")

    for where, text in [("svg <title>", summary.title), ("svg <desc>", summary.desc)] + [
        ("svg text", t) for t in summary.all_text
    ]:
        match = PLACEHOLDER_RE.search(text)
        if match:
            errors.append(
                f"placeholder text {match.group(0)!r} left in {where} ({text!r}); "
                "replace it with real domain wording"
            )

    check_expected_terms(summary, expect, errors)

    if summary.duplicate_node_labels:
        warnings.append(
            f"duplicate node label(s) found: {', '.join(summary.duplicate_node_labels)}"
        )
    if summary.nodes and summary.desc.strip():
        desc_low = summary.desc.casefold()
        if not any(n.label.casefold() in desc_low for n in summary.nodes if len(n.label) >= 3):
            warnings.append("diagram <desc> does not mention any detected node label")

    if strict:
        errors.extend(warnings)
        warnings = []
    return errors, warnings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file", type=Path, help="Diagram .html or .svg file to describe")
    ap.add_argument("--json", action="store_true", help="emit structured JSON instead of text")
    ap.add_argument(
        "--check",
        action="store_true",
        help="run semantic hygiene checks (placeholders, title/desc quality, --expect terms)",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="with --check, treat semantic warnings (e.g. duplicate node labels) as errors",
    )
    ap.add_argument(
        "--expect",
        action="append",
        default=[],
        metavar="TERM",
        help="require TERM (case-insensitive) in diagram text/title/desc (repeatable)",
    )
    args = ap.parse_args(argv)

    if not args.file.is_file():
        print(f"error: file not found: {args.file}", file=sys.stderr)
        return 2
    try:
        source = args.file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        print(f"error: cannot read {args.file}: {exc}", file=sys.stderr)
        return 2

    summary = describe_document(source)
    if summary.viewbox is None and not summary.title and not summary.all_text:
        print(f"error: no <svg> diagram content found in {args.file}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(summary_to_dict(summary), indent=2))
    else:
        print(format_summary(summary))

    if args.check or args.expect:
        errors, warnings = check_semantics(summary, args.expect, strict=args.strict)
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
        if errors:
            for error in errors:
                print(f"FAIL: {error}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
