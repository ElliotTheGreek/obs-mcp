"""Camera-animation timeline: a list of keyframes that move and scale the camera
over the screen, sampled per-frame with easing.

A keyframe places the camera at time ``t`` as an aspect-preserved box, described by:
- ``scale``: fraction of the OUTPUT FRAME HEIGHT the camera occupies. 1.0 == the
  camera is as tall as the frame (fullscreen for a matching aspect); ~0.28 is a
  typical picture-in-picture.
- ``anchor``: where the box sits -- ``center``, ``top-left``, ``top-right``,
  ``bottom-left``, ``bottom-right`` -- with a small margin from the edges. OR
- ``pos``: explicit normalized center ``[nx, ny]`` in 0..1 (overrides anchor).
- ``ease``: interpolation INTO this keyframe -- ``inout`` (default), ``linear``,
  ``in``, ``out``.

Shorthand ``preset`` expands to (scale, anchor):
  fullscreen -> (1.0, center); top-left/top-right/bottom-left/bottom-right ->
  (PIP, that corner); center/pip -> (PIP, center).

The agent authors these from the user's description; the compositor renders them.
"""

from __future__ import annotations

from typing import Any

PIP_SCALE = 0.28          # default picture-in-picture size (fraction of frame height)
MARGIN = 0.03             # gap from frame edge for corner anchors (normalized)

PRESETS: dict[str, dict[str, Any]] = {
    "fullscreen": {"scale": 1.0, "anchor": "center"},
    "full": {"scale": 1.0, "anchor": "center"},
    "center": {"scale": PIP_SCALE, "anchor": "center"},
    "pip": {"scale": PIP_SCALE, "anchor": "bottom-right"},
    "top-left": {"scale": PIP_SCALE, "anchor": "top-left"},
    "top-right": {"scale": PIP_SCALE, "anchor": "top-right"},
    "bottom-left": {"scale": PIP_SCALE, "anchor": "bottom-left"},
    "bottom-right": {"scale": PIP_SCALE, "anchor": "bottom-right"},
}

ANCHORS = {"center", "top-left", "top-right", "bottom-left", "bottom-right"}


def normalize_keyframes(keyframes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate + expand presets into canonical keyframes sorted by time. Each
    canonical keyframe has: t, scale, anchor|pos, ease."""
    if not keyframes:
        raise ValueError("timeline has no keyframes")
    out: list[dict[str, Any]] = []
    for i, kf in enumerate(keyframes):
        k = dict(kf)
        if "preset" in k:
            preset = k.pop("preset")
            if preset not in PRESETS:
                raise ValueError(
                    f"keyframe {i}: unknown preset {preset!r}; "
                    f"valid: {', '.join(sorted(PRESETS))}"
                )
            for key, val in PRESETS[preset].items():
                k.setdefault(key, val)
        if "t" not in k:
            raise ValueError(f"keyframe {i} is missing 't' (seconds)")
        k["t"] = float(k["t"])
        k["scale"] = float(k.get("scale", PIP_SCALE))
        k["ease"] = k.get("ease", "inout")
        if "pos" in k:
            nx, ny = k["pos"]
            k["pos"] = [float(nx), float(ny)]
            k.pop("anchor", None)
        else:
            anchor = k.get("anchor", "center")
            if anchor not in ANCHORS:
                raise ValueError(
                    f"keyframe {i}: anchor must be one of {sorted(ANCHORS)} "
                    f"(or use pos/preset), got {anchor!r}"
                )
            k["anchor"] = anchor
        out.append(k)
    out.sort(key=lambda k: k["t"])
    return out


def _ease(p: float, kind: str) -> float:
    p = max(0.0, min(1.0, p))
    if kind == "linear":
        return p
    if kind == "in":
        return p * p
    if kind == "out":
        return 1.0 - (1.0 - p) ** 2
    # default smooth ease-in-out (smoothstep)
    return p * p * (3.0 - 2.0 * p)


def _center(kf: dict[str, Any], draw_w: float, draw_h: float,
            fw: float, fh: float) -> tuple[float, float]:
    """Pixel center of the camera box for a keyframe, given its drawn size."""
    if "pos" in kf:
        return kf["pos"][0] * fw, kf["pos"][1] * fh
    anchor = kf["anchor"]
    m = MARGIN * fh
    cx = fw / 2.0
    cy = fh / 2.0
    if "left" in anchor:
        cx = m + draw_w / 2.0
    elif "right" in anchor:
        cx = fw - m - draw_w / 2.0
    if "top" in anchor:
        cy = m + draw_h / 2.0
    elif "bottom" in anchor:
        cy = fh - m - draw_h / 2.0
    return cx, cy


def _rect_at_kf(kf: dict[str, Any], fw: float, fh: float,
                cam_aspect: float) -> tuple[float, float, float, float]:
    """(x, y, w, h) pixel rect of the aspect-preserved camera for one keyframe."""
    draw_h = kf["scale"] * fh
    draw_w = draw_h * cam_aspect
    cx, cy = _center(kf, draw_w, draw_h, fw, fh)
    return cx - draw_w / 2.0, cy - draw_h / 2.0, draw_w, draw_h


def sample(keyframes: list[dict[str, Any]], t: float, fw: int, fh: int,
           cam_aspect: float) -> tuple[int, int, int, int]:
    """Interpolate the camera rect (x, y, w, h) in pixels at time ``t``."""
    kfs = keyframes
    if t <= kfs[0]["t"]:
        r = _rect_at_kf(kfs[0], fw, fh, cam_aspect)
        return _ints(r)
    if t >= kfs[-1]["t"]:
        r = _rect_at_kf(kfs[-1], fw, fh, cam_aspect)
        return _ints(r)
    for a, b in zip(kfs, kfs[1:]):
        if a["t"] <= t <= b["t"]:
            span = b["t"] - a["t"]
            p = 0.0 if span <= 0 else (t - a["t"]) / span
            e = _ease(p, b["ease"])
            ra = _rect_at_kf(a, fw, fh, cam_aspect)
            rb = _rect_at_kf(b, fw, fh, cam_aspect)
            return _ints(tuple(ra[i] + (rb[i] - ra[i]) * e for i in range(4)))
    r = _rect_at_kf(kfs[-1], fw, fh, cam_aspect)
    return _ints(r)


def _ints(r: tuple[float, ...]) -> tuple[int, int, int, int]:
    # width/height must be >=2 and even-ish for encoders; clamp to >=2
    x, y, w, h = r
    return int(round(x)), int(round(y)), max(2, int(round(w))), max(2, int(round(h)))


# -- overlay graphics (arrows/rings/labels/images) ---------------------------

def normalize_overlay_keyframes(keyframes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Canonicalize overlay keyframes: each needs t, pos [nx, ny] (normalized screen
    coords the ANCHOR lands on), scale (overlay width as a fraction of frame width),
    ease. Sorted by time."""
    if not keyframes:
        raise ValueError("overlay has no keyframes")
    out = []
    for i, kf in enumerate(keyframes):
        k = dict(kf)
        if "t" not in k:
            raise ValueError(f"overlay keyframe {i} missing 't'")
        if "pos" not in k:
            raise ValueError(f"overlay keyframe {i} missing 'pos' [nx, ny]")
        k["t"] = float(k["t"])
        k["pos"] = [float(k["pos"][0]), float(k["pos"][1])]
        k["scale"] = float(k.get("scale", 0.12))
        k["ease"] = k.get("ease", "inout")
        out.append(k)
    out.sort(key=lambda k: k["t"])
    return out


def sample_overlay(keyframes: list[dict[str, Any]], t: float, fw: int, fh: int,
                   aspect: float, anchor: list[float]) -> tuple[int, int, int, int]:
    """Interpolate an overlay's pixel rect (x, y, w, h) at time t. `aspect` = the
    graphic's width/height; `anchor` = [ax, ay] point on the graphic placed at pos."""
    kfs = keyframes

    def at(kf: dict[str, Any]) -> tuple[float, float, float, float]:
        draw_w = kf["scale"] * fw
        draw_h = draw_w / aspect
        x = kf["pos"][0] * fw - anchor[0] * draw_w
        y = kf["pos"][1] * fh - anchor[1] * draw_h
        return x, y, draw_w, draw_h

    if t <= kfs[0]["t"]:
        return _ints(at(kfs[0]))
    if t >= kfs[-1]["t"]:
        return _ints(at(kfs[-1]))
    for a, b in zip(kfs, kfs[1:]):
        if a["t"] <= t <= b["t"]:
            span = b["t"] - a["t"]
            p = 0.0 if span <= 0 else (t - a["t"]) / span
            e = _ease(p, b["ease"])
            ra, rb = at(a), at(b)
            return _ints(tuple(ra[i] + (rb[i] - ra[i]) * e for i in range(4)))
    return _ints(at(kfs[-1]))
