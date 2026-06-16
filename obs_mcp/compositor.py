"""Composite the isolated camera over the clean screen recording, animating the
camera's position and scale from a keyframe timeline, and write a final mp4 ready
for short-form-editor.

Audio: the screen recording carries the spoken audio (mic is on its main track), so
we take audio from the screen file and drop the camera file's duplicate audio.
"""

from __future__ import annotations

import os
from typing import Any

from . import timeline


def compose(
    screen_path: str,
    camera_path: str,
    keyframes: list[dict[str, Any]],
    output_path: str,
    remove_background: bool = False,
    max_duration: float | None = None,
) -> dict[str, Any]:
    """Render screen + animated camera -> output_path. ``keyframes`` is the raw
    timeline (presets allowed); it is normalized here. ``max_duration`` optionally
    caps the output length. Returns output info."""
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

    final = CompositeVideoClip([screen, cam_layer], size=(fw, fh)).with_duration(duration)
    if screen.audio is not None:
        final = final.with_audio(screen.audio.subclipped(0, duration))

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    final.write_videofile(
        output_path, codec="libx264", audio_codec="aac",
        fps=screen.fps, preset="medium", threads=os.cpu_count() or 4,
        logger=None,
    )
    for clip in (final, cam_layer, camera, screen):
        try:
            clip.close()
        except Exception:  # noqa: BLE001
            pass
    return {
        "output": output_path,
        "size": [fw, fh],
        "duration": round(duration, 2),
        "keyframes": len(kfs),
        "background_removed": remove_background,
    }


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
