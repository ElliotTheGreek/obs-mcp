"""Screen blur effects: blur a region, or focus an area by blurring everything
outside a shape. Both are one operation -- "blur where the mask is 1":

- region blur (redact): mask = the shape (rect / circle / svg silhouette)
- focus / spotlight: mask = INVERTED shape (blur outside, keep the shape sharp)

Shapes animate over time via overlay-style keyframes (pos = normalized centre,
scale = width as a fraction of frame width) and fade in/out over [t_in, t_out].
Applied to the screen layer, so the camera and graphic overlays stay sharp on top.
"""

from __future__ import annotations

from typing import Any, Callable

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import timeline


def _amp(spec: dict[str, Any], t: float, duration: float) -> float:
    """Fade amplitude of a blur at time t (0 outside its window, ramps over `fade`)."""
    kfs = spec["_kfs"]
    t_in = float(spec.get("t_in", kfs[0]["t"]))
    t_out = float(spec.get("t_out", duration))
    if t < t_in or t > t_out:
        return 0.0
    fade = float(spec.get("fade", 0.3))
    if fade <= 0:
        return 1.0
    a = min((t - t_in) / fade, (t_out - t) / fade, 1.0)
    return max(0.0, a)


def _mask(spec: dict[str, Any], rect: tuple[int, int, int, int],
          fw: int, fh: int) -> np.ndarray:
    """Build a HxW float mask (1 where blur applies) for one shape at `rect`."""
    x, y, w, h = rect
    img = Image.new("L", (fw, fh), 0)
    shape = spec.get("shape", "circle")
    if shape == "rect":
        ImageDraw.Draw(img).rectangle([x, y, x + w, y + h], fill=255)
    elif shape == "circle":
        ImageDraw.Draw(img).ellipse([x, y, x + w, y + h], fill=255)
    elif shape == "svg":
        from . import graphics
        arr = graphics.render_svg(spec["svg"], max(8, w))
        alpha = Image.fromarray(arr[:, :, 3]).resize((max(1, w), max(1, h)))
        img.paste(alpha, (x, y))
    else:
        raise ValueError(f"blur shape must be rect|circle|svg, got {shape!r}")

    feather = float(spec.get("feather", 10))
    if feather > 0:
        img = img.filter(ImageFilter.GaussianBlur(feather))
    m = np.asarray(img, dtype=np.float32) / 255.0
    if spec.get("invert", False):  # focus: blur OUTSIDE the shape
        m = 1.0 - m
    return m


def make_processor(blurs: list[dict[str, Any]], fw: int, fh: int,
                   duration: float) -> Callable:
    """Return a moviepy frame transform (get_frame, t) -> frame that applies all
    blur specs to the screen frame at time t."""
    specs = []
    for s in blurs:
        s = dict(s)
        s["_kfs"] = timeline.normalize_overlay_keyframes(s["keyframes"])
        specs.append(s)

    def process(get_frame, t):  # type: ignore[no-untyped-def]
        frame = get_frame(t).astype(np.float32)  # HxWx3
        for s in specs:
            amp = _amp(s, t, duration)
            if amp <= 0:
                continue
            rect = timeline.sample_overlay(
                s["_kfs"], t, fw, fh, float(s.get("aspect", 1.0)), [0.5, 0.5])
            m = (_mask(s, rect, fw, fh) * amp)[:, :, None]
            radius = float(s.get("strength", 18))
            blurred = np.asarray(
                Image.fromarray(np.clip(frame, 0, 255).astype(np.uint8))
                .filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32)
            frame = frame * (1 - m) + blurred * m
            dim = float(s.get("dim", 0.0))  # optionally darken the blurred area
            if dim > 0:
                frame = frame * (1 - m * dim)
        return np.clip(frame, 0, 255).astype(np.uint8)

    return process
