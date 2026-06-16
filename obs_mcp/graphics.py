"""SVG overlay graphics: render SVG -> RGBA, plus parametric built-in pointer shapes
whose ANCHOR (the point that lands on the screen target, e.g. an arrow's tip) is
defined so the agent can aim them at located screen coordinates.

Each builder returns (svg_string, anchor) where anchor is [ax, ay] in 0..1 of the
graphic's own box -- the point placed at the keyframe position.
"""

from __future__ import annotations

import io
import math
from typing import Any

import numpy as np
import resvg_py
from PIL import Image


def render_svg(svg: str, width: int) -> np.ndarray:
    """Rasterize an SVG to an RGBA uint8 array (H, W, 4) at the given pixel width
    (height follows the SVG aspect). resvg = crisp, full SVG + text support."""
    png = resvg_py.svg_to_bytes(svg_string=svg, width=int(max(8, width)))
    img = Image.open(io.BytesIO(bytes(png))).convert("RGBA")
    return np.array(img)


# -- built-in pointer shapes -------------------------------------------------

def arrow(direction: str = "down-right", color: str = "#ff3b30",
          stroke: int = 12) -> tuple[str, list[float]]:
    """An arrow whose TIP is the anchor, pointing in `direction` (8 compass names
    or an angle in degrees as a string). The tip lands on the target; the shaft
    trails away from it."""
    deg = _DIR_DEG.get(direction)
    if deg is None:
        deg = float(direction)  # allow an explicit angle
    a = math.radians(deg)
    cx = cy = 50.0
    tip = (cx + 45 * math.cos(a), cy + 45 * math.sin(a))
    tail = (cx - 40 * math.cos(a), cy - 40 * math.sin(a))
    # arrowhead: two barbs behind the tip
    back = (tip[0] - 26 * math.cos(a), tip[1] - 26 * math.sin(a))
    perp = (-math.sin(a), math.cos(a))
    b1 = (back[0] + 16 * perp[0], back[1] + 16 * perp[1])
    b2 = (back[0] - 16 * perp[0], back[1] - 16 * perp[1])
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <line x1="{tail[0]:.1f}" y1="{tail[1]:.1f}" x2="{back[0]:.1f}" y2="{back[1]:.1f}"
        stroke="{color}" stroke-width="{stroke}" stroke-linecap="round"/>
  <polygon points="{tip[0]:.1f},{tip[1]:.1f} {b1[0]:.1f},{b1[1]:.1f} {b2[0]:.1f},{b2[1]:.1f}"
        fill="{color}"/>
</svg>"""
    return svg, [tip[0] / 100.0, tip[1] / 100.0]


def ring(color: str = "#ff3b30", stroke: int = 8) -> tuple[str, list[float]]:
    """A circular highlight ring; anchor = center (place around a target)."""
    r = 50 - stroke
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <circle cx="50" cy="50" r="{r}" fill="none" stroke="{color}" stroke-width="{stroke}"/>
</svg>"""
    return svg, [0.5, 0.5]


def box(color: str = "#ff3b30", stroke: int = 8, aspect: float = 1.6,
        radius: int = 6) -> tuple[str, list[float]]:
    """A rounded rectangle outline to frame a screen region; anchor = center.
    `aspect` = width/height of the box."""
    w, h = 100.0, 100.0 / aspect
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.1f} {h:.1f}">
  <rect x="{stroke/2:.1f}" y="{stroke/2:.1f}" width="{w-stroke:.1f}" height="{h-stroke:.1f}"
        rx="{radius}" fill="none" stroke="{color}" stroke-width="{stroke}"/>
</svg>"""
    return svg, [0.5, 0.5]


def label(text: str, color: str = "#ff3b30", text_color: str = "#ffffff",
          font_size: int = 30) -> tuple[str, list[float]]:
    """A rounded pill with text; anchor = center. Width follows the text length."""
    pad = font_size * 0.8
    w = max(80.0, len(text) * font_size * 0.62 + pad * 2)
    h = font_size + pad
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.0f} {h:.0f}">
  <rect x="2" y="2" width="{w-4:.0f}" height="{h-4:.0f}" rx="{h/2:.0f}" fill="{color}"/>
  <text x="{w/2:.0f}" y="{h/2:.0f}" fill="{text_color}" font-family="Arial, sans-serif"
        font-size="{font_size}" font-weight="700" text-anchor="middle"
        dominant-baseline="central">{_xml_escape(text)}</text>
</svg>"""
    return svg, [0.5, 0.5]


BUILDERS = {"arrow": arrow, "ring": ring, "box": box, "label": label}

_DIR_DEG = {
    "right": 0, "down-right": 45, "down": 90, "down-left": 135,
    "left": 180, "up-left": 225, "up": 270, "up-right": 315,
}


def build(spec: dict[str, Any]) -> tuple[str, list[float]]:
    """Resolve an overlay's graphic to (svg, default_anchor). `spec` is either
    {"svg": "<inline svg>"} or {"kind": "arrow"|"ring"|"box"|"label", **params}."""
    if "svg" in spec:
        return spec["svg"], spec.get("anchor", [0.5, 0.5])
    kind = spec.get("kind")
    if kind not in BUILDERS:
        raise ValueError(f"unknown graphic kind {kind!r}; valid: {list(BUILDERS)} or pass 'svg'")
    params = {k: v for k, v in spec.items() if k not in _RESERVED}
    svg, anchor = BUILDERS[kind](**params)
    return svg, spec.get("anchor", anchor)


# overlay-level keys consumed by the compositor, never passed to a shape builder
_RESERVED = {"kind", "anchor", "svg", "image", "keyframes", "t_in", "t_out",
             "fade", "opacity"}


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))
