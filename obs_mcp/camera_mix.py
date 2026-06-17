"""GPU camera mixing for the long-form workflow.

Given a project whose SCREEN recording has been cut to an EDL, mix the separately
recorded camera back in, background-removed, floating over the cut screen:

  1. cut the camera to the SAME segment times as the screen cut (perfect sync),
  2. matte out the background with RobustVideoMatting on the GPU,
  3. composite the floating camera over the cut screen with animated position.

The expensive cut+matte is CACHED per (project, edl), so repositioning ("move me
over here for this part") only re-runs the cheap composite step.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Any

from . import timeline

_RVM = None


def _load_rvm():
    """Lazy-load RobustVideoMatting on the GPU (cached locally after first download)."""
    global _RVM
    if _RVM is None:
        import torch
        _RVM = torch.hub.load("PeterL1n/RobustVideoMatting", "mobilenetv3").cuda().eval()
    return _RVM


def _resolve_segments(project_id: str, edl_id: str) -> list[tuple[float, float]]:
    """The exact (start, end) source-time segments the screen render kept -- recomputed
    deterministically (same cleanup + resolve the renderer ran)."""
    from short_form_editor_mcp import edl as edlmod
    from short_form_editor_mcp import workspace as ws
    transcript = ws.read_json(ws.transcript_json_path(project_id))
    edl_obj = ws.read_json(ws.edl_path(project_id, edl_id))
    cleaned = edl_obj
    if edl_obj.get("cleanup"):
        cleaned, _ = edlmod.apply_cleanup(edl_obj, transcript)
    resolved = edlmod.resolve_edl(cleaned, transcript)
    return [(float(s["start"]), float(s["end"])) for s in resolved["segments"]]


def cut_video_only(src: str, segments: list[tuple[float, float]], out: str) -> None:
    """Trim `src` to `segments` and concat (video only -- audio comes from the screen).
    Uses a filter_complex SCRIPT file so hundreds of micro-cuts don't blow the cmdline."""
    parts, labels = [], []
    for i, (s, e) in enumerate(segments):
        parts.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]")
        labels.append(f"[v{i}]")
    graph = ";".join(parts) + ";" + "".join(labels) + \
        f"concat=n={len(segments)}:v=1:a=0[outv]"
    fd, scriptf = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    with open(scriptf, "w", encoding="utf-8") as f:
        f.write(graph)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", src,
             "-filter_complex_script", scriptf, "-map", "[outv]", "-an",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", out], check=True)
    finally:
        os.remove(scriptf)


def _ffmpeg_frame_writer(out: str, w: int, h: int, fps: float, gray: bool, crf: int):
    pix = "gray" if gray else "rgb24"
    return subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", pix,
         "-s", f"{w}x{h}", "-r", f"{fps:.4f}", "-i", "-",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
         "-pix_fmt", "yuv420p", out], stdin=subprocess.PIPE)


def rvm_matte(src: str, fgr_out: str, pha_out: str,
              downsample: float = 0.4, chunk: int = 12) -> None:
    """RVM GPU matte: write the foreground (fgr_out) and the alpha (pha_out) as videos,
    streaming so memory stays flat on long clips."""
    import cv2
    import numpy as np
    import torch

    model = _load_rvm()
    cap = cv2.VideoCapture(src)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    wf = _ffmpeg_frame_writer(fgr_out, w, h, fps, gray=False, crf=18)
    wp = _ffmpeg_frame_writer(pha_out, w, h, fps, gray=True, crf=12)
    rec: list = [None] * 4
    buf: list = []

    def flush():
        nonlocal rec
        if not buf:
            return
        t = (torch.from_numpy(np.stack(buf)).cuda().float().div(255)
             .permute(0, 3, 1, 2).unsqueeze(0))  # [1,T,3,H,W] RGB
        with torch.no_grad():
            fgr, pha, *rec = model(t, *rec, downsample)
        fgr_np = (fgr[0].clamp(0, 1).cpu().numpy().transpose(0, 2, 3, 1) * 255).astype("uint8")
        pha_np = (pha[0, :, 0].clamp(0, 1).cpu().numpy() * 255).astype("uint8")
        for k in range(fgr_np.shape[0]):
            wf.stdin.write(fgr_np[k].tobytes())     # RGB
            wp.stdin.write(pha_np[k].tobytes())     # gray
        buf.clear()

    try:
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            buf.append(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB))
            if len(buf) >= chunk:
                flush()
        flush()
    finally:
        cap.release()
        for p in (wf, wp):
            p.stdin.close()
            p.wait()


def composite(screen_path: str, fgr_path: str, pha_path: str | None,
              keyframes: list[dict[str, Any]], out_path: str) -> dict[str, Any]:
    """Composite the (matted) camera over the cut screen with animated position. Cheap
    relative to the matte -- this is what reposition re-runs."""
    from moviepy import CompositeVideoClip, VideoFileClip

    screen = VideoFileClip(screen_path)
    fgr = VideoFileClip(fgr_path)
    cam = fgr.without_audio()
    if pha_path:
        cam = cam.with_mask(VideoFileClip(pha_path).to_mask())
    fw, fh = screen.size
    aspect = cam.w / cam.h
    kfs = timeline.normalize_keyframes(keyframes)
    dur = min(screen.duration, cam.duration)

    def rect(t: float):
        return timeline.sample(kfs, t, fw, fh, aspect)

    cam_layer = (cam.resized(lambda t: (rect(t)[2], rect(t)[3]))
                 .with_position(lambda t: (rect(t)[0], rect(t)[1])))
    final = CompositeVideoClip([screen, cam_layer], size=(fw, fh)).with_duration(dur)
    if screen.audio is not None:
        final = final.with_audio(screen.audio.subclipped(0, dur))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    final.write_videofile(out_path, codec="libx264", audio_codec="aac",
                          fps=screen.fps, preset="medium",
                          threads=os.cpu_count() or 4, logger=None)
    for c in (final, cam_layer, cam, fgr, screen):
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
    return {"output": out_path, "size": [fw, fh], "duration": round(dur, 2)}


def mix_camera(project_id: str, edl_id: str, camera_path: str,
               keyframes: list[dict[str, Any]] | None = None,
               remove_background: bool = True, output_path: str = "",
               rematte: bool = False) -> dict[str, Any]:
    """Mix the camera into a project's cut screen. The cut + matte are cached per
    (project, edl); pass rematte=True to force a rebuild (e.g. after changing the cut)."""
    from short_form_editor_mcp import workspace as ws

    pdir = ws.project_dir(project_id)
    slug = ws.slugify(edl_id)
    cut_screen = pdir / "renders" / f"{slug}.mp4"
    if not cut_screen.exists():
        raise FileNotFoundError(
            f"cut screen not found at {cut_screen}. Run render(project_id, edl_id='{edl_id}') first.")
    if not os.path.isfile(camera_path):
        raise FileNotFoundError(f"camera file not found: {camera_path}")

    cam_dir = pdir / "camera"
    cam_dir.mkdir(exist_ok=True)
    cut_cam = cam_dir / f"{slug}_cut.mp4"
    if rematte or not cut_cam.exists():
        cut_video_only(camera_path, _resolve_segments(project_id, edl_id), str(cut_cam))

    cached = False
    if remove_background:
        fgr = cam_dir / f"{slug}_fgr.mp4"
        pha = cam_dir / f"{slug}_pha.mp4"
        if rematte or not (fgr.exists() and pha.exists()):
            rvm_matte(str(cut_cam), str(fgr), str(pha))
        else:
            cached = True
        src_fgr, src_pha = str(fgr), str(pha)
    else:
        src_fgr, src_pha = str(cut_cam), None

    if not keyframes:
        keyframes = [{"t": 0, "preset": "bottom-right"}]
    out = output_path or str(pdir / "renders" / f"{slug}_mixed.mp4")
    info = composite(str(cut_screen), src_fgr, src_pha, keyframes, out)
    info.update({"project_id": project_id, "edl_id": edl_id,
                 "background_removed": remove_background, "matte_cached": cached})
    return info
