"""Composite the isolated camera over the clean screen recording, animating the
camera's position and scale from a keyframe timeline, and write a final mp4 ready
for short-form-editor.

Audio: the screen recording carries the spoken audio (mic is on its main track), so
we take audio from the screen file and drop the camera file's duplicate audio.
"""

from __future__ import annotations

import os
from typing import Any

from . import graphics, timeline


def compose(
    screen_path: str,
    camera_path: str,
    keyframes: list[dict[str, Any]],
    output_path: str,
    remove_background: bool = False,
    max_duration: float | None = None,
    overlays: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Render screen + animated camera (+ optional animated graphic overlays) ->
    output_path. ``keyframes`` is the raw camera timeline (presets allowed).
    ``overlays`` is a list of animated graphic specs (see _build_overlay).
    ``max_duration`` optionally caps the output length. Returns output info."""
    for p, label in ((screen_path, "screen"), (camera_path, "camera")):
        if not os.path.isfile(p):
            raise FileNotFoundError(f"{label} file not found: {p}")

    kfs = timeline.normalize_keyframes(keyframes)

    # import moviepy lazily so the OBS-control half of the package works without it
    from moviepy import CompositeVideoClip, VideoFileClip

    screen = VideoFileClip(screen_path)
    camera = VideoFileClip(camera_path).without_audio()
    fw, fh = screen.size
    cam_aspect = camera.w / camera.h
    duration = min(screen.duration, camera.duration)
    if max_duration is not None:
        duration = min(duration, max_duration)

    if remove_background:
        camera = _matte(camera)

    def rect(t: float) -> tuple[int, int, int, int]:
        return timeline.sample(kfs, t, fw, fh, cam_aspect)

    cam_layer = (
        camera.resized(lambda t: (rect(t)[2], rect(t)[3]))
        .with_position(lambda t: (rect(t)[0], rect(t)[1]))
    )

    layers = [screen, cam_layer]
    overlay_clips = [_build_overlay(o, fw, fh, duration) for o in (overlays or [])]
    layers.extend(overlay_clips)  # overlays render on top of the camera

    final = CompositeVideoClip(layers, size=(fw, fh)).with_duration(duration)
    if screen.audio is not None:
        final = final.with_audio(screen.audio.subclipped(0, duration))

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    final.write_videofile(
        output_path, codec="libx264", audio_codec="aac",
        fps=screen.fps, preset="medium", threads=os.cpu_count() or 4,
        logger=None,
    )
    for clip in [final, cam_layer, camera, screen, *overlay_clips]:
        try:
            clip.close()
        except Exception:  # noqa: BLE001
            pass
    return {
        "output": output_path,
        "size": [fw, fh],
        "duration": round(duration, 2),
        "keyframes": len(kfs),
        "overlays": len(overlay_clips),
        "background_removed": remove_background,
    }


def _load_rgba(path: str):  # type: ignore[no-untyped-def]
    import numpy as np
    from PIL import Image
    if not os.path.isfile(path):
        raise FileNotFoundError(f"overlay image not found: {path}")
    return np.array(Image.open(path).convert("RGBA"))


def _build_overlay(spec: dict[str, Any], fw: int, fh: int, duration: float):  # type: ignore[no-untyped-def]
    """Build one animated overlay layer.

    spec source (one of):
      - {"kind": "arrow"|"ring"|"box"|"label", ...params}  (built-in shape)
      - {"svg": "<inline svg>"}                            (custom SVG)
      - {"image": "path.png"}                              (pre-rendered RGBA, e.g.
                                                            a Gemini-generated graphic)
    plus: anchor [ax,ay] (point placed on pos; defaults per shape / center),
    keyframes [{t,pos,scale,ease}], t_in, t_out, fade (s), opacity.
    """
    from moviepy import ImageClip
    from moviepy.video.fx import CrossFadeIn, CrossFadeOut

    kfs = timeline.normalize_overlay_keyframes(spec["keyframes"])
    if "image" in spec:
        arr = _load_rgba(spec["image"])
        anchor = spec.get("anchor", [0.5, 0.5])
    else:
        svg, anchor = graphics.build(spec)
        base_w = max(k["scale"] for k in kfs) * fw
        arr = graphics.render_svg(svg, int(min(max(base_w * 2, 64), 2 * fw)))
    oh, ow = arr.shape[0], arr.shape[1]
    aspect = ow / oh

    t_in = max(0.0, min(float(spec.get("t_in", kfs[0]["t"])), duration))
    t_out = max(t_in + 0.1, min(float(spec.get("t_out", duration)), duration))

    base = ImageClip(arr[:, :, :3]).with_mask(ImageClip(arr[:, :, 3] / 255.0, is_mask=True))

    def r(local_t: float) -> tuple[int, int, int, int]:
        return timeline.sample_overlay(kfs, t_in + local_t, fw, fh, aspect, anchor)

    layer = (
        base.with_start(t_in).with_duration(t_out - t_in)
        .resized(lambda lt: (r(lt)[2], r(lt)[3]))
        .with_position(lambda lt: (r(lt)[0], r(lt)[1]))
    )
    opacity = float(spec.get("opacity", 1.0))
    if opacity < 1.0:
        layer = layer.with_opacity(opacity)
    fade = min(float(spec.get("fade", 0.3)), (t_out - t_in) / 2.0)
    if fade > 0:
        layer = layer.with_effects([CrossFadeIn(fade), CrossFadeOut(fade)])
    return layer


def _matte(camera_clip):  # type: ignore[no-untyped-def]
    """Background-removed camera (floating subject) as an alpha-masked clip. Runs the
    matting model ONCE per frame-time (cached) even though the colour and mask layers
    both sample it. Lazy-imports rembg; raises a clear hint if the matting extra is
    not installed."""
    try:
        from rembg import new_session, remove
    except ImportError as e:  # noqa: BLE001
        raise RuntimeError(
            "Background removal needs the matting extra. Install with: "
            "pip install -e E:\\FlowdotPlatform\\obs-mcp[matting]"
        ) from e
    from moviepy import VideoClip

    session = new_session("u2net_human_seg")
    cache: dict[float, Any] = {}

    def rgba(t: float):  # HxWx4 uint8 cutout at time t, cached
        key = round(float(t), 4)
        cut = cache.get(key)
        if cut is None:
            if len(cache) > 8:
                cache.clear()
            cut = remove(camera_clip.get_frame(t), session=session)
            cache[key] = cut
        return cut

    colour = camera_clip.transform(lambda gf, t: rgba(t)[:, :, :3])
    mask = VideoClip(frame_function=lambda t: rgba(t)[:, :, 3] / 255.0, is_mask=True)
    mask = mask.with_duration(camera_clip.duration)
    mask.fps = camera_clip.fps
    return colour.with_mask(mask)
