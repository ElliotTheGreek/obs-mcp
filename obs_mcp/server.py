"""FastMCP server: drive OBS for track-separated recording, then composite an
animated camera over the screen and hand the result to short-form-editor.

Stage 1 (this file's tools): enumerate devices, set up a verified track-separated
scene, record, and report the screen+camera file pair for each take.
Stage 2 (compositor): see compose_camera once compositor.py lands.
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import compositor, config
from .obs_client import Device, OBSClient
from .ws import OBSError

mcp = FastMCP("obs-mcp")

_client: OBSClient | None = None


def obs() -> OBSClient:
    global _client
    if _client is None:
        _client = OBSClient(
            host=config.OBS_WS_HOST, port=config.OBS_WS_PORT,
            password=config.OBS_WS_PASSWORD,
        )
    return _client


def _resolve(query: str, devices: list[Device], kind: str) -> Device:
    """Match a user query (case-insensitive substring, or exact device id) to one
    device. Raise a clear error listing options on no/ambiguous match."""
    exact = [d for d in devices if d.device_id == query]
    if exact:
        return exact[0]
    hits = [d for d in devices if query.lower() in d.name.lower()]
    if len(hits) == 1:
        return hits[0]
    names = ", ".join(d.name for d in devices) or "(none found)"
    if not hits:
        raise OBSError(f"No {kind} matches {query!r}. Available {kind}s: {names}")
    raise OBSError(
        f"{query!r} matches multiple {kind}s ({', '.join(h.name for h in hits)}); "
        "be more specific."
    )


@mcp.tool()
def list_devices() -> dict[str, Any]:
    """List the OBS-visible monitors, cameras, and microphones available to record.
    Returns each as {name, device_id}. OBS must be running with its WebSocket server
    enabled."""
    c = obs()
    def dump(ds: list[Device]) -> list[dict[str, str]]:
        return [{"name": d.name, "device_id": d.device_id} for d in ds]
    return {
        "monitors": dump(c.list_monitors()),
        "cameras": dump([d for d in c.list_cameras() if "OBS Virtual Camera" not in d.name]),
        "mics": dump(c.list_mics()),
    }


@mcp.tool()
def setup_scene(monitor: str, camera: str, mic: str, mic_track: int = 1) -> dict[str, Any]:
    """Set up a track-separated recording scene and PROVE it captures before returning.

    monitor/camera/mic accept a case-insensitive name substring (e.g. "Primary",
    "NexiGo", "JOUNIVO") or an exact device_id from list_devices.

    Builds: full-screen capture of `monitor` (the clean screen recording) + `camera`
    recorded to its OWN isolated file via Source Record (off-canvas, never baked into
    the screen) + `mic` on its own audio track. Idempotent: re-calling with the same
    devices does not reopen the camera. Runs a throwaway test recording to verify the
    isolated camera file actually records, retrying until proven.
    """
    c = obs()
    if not c.has_source_record():
        raise OBSError(
            "Source Record plugin is not loaded in OBS. Install it from "
            "https://github.com/exeldro/obs-source-record/releases and restart OBS."
        )
    mon = _resolve(monitor, c.list_monitors(), "monitor")
    cam = _resolve(camera, c.list_cameras(), "camera")
    micd = _resolve(mic, c.list_mics(), "mic")

    os.makedirs(config.CAMERA_DIR, exist_ok=True)
    info = c.build_scene(config.SCENE_NAME, mon.device_id, cam.device_id,
                         micd.device_id, config.CAMERA_DIR, mic_track=mic_track)
    verified = c.verify_camera_capture(config.CAMERA_DIR)
    if not verified:
        raise OBSError(
            "Scene built but the isolated camera file did not record after retries. "
            "Check the Camera source is producing frames in OBS (a USB webcam can "
            "stall as 'Insufficient system resources' if opened repeatedly; unplug/"
            "replug or pick a different camera)."
        )
    return {
        "ready": True, "scene": info["scene"], "rebuilt": info["rebuilt"],
        "monitor": mon.name, "camera": cam.name, "mic": micd.name,
        "mic_track": mic_track, "camera_dir": config.CAMERA_DIR,
        "note": "Verified: screen and camera record to separate files.",
    }


@mcp.tool()
def start_recording() -> dict[str, Any]:
    """Start recording. Captures the screen to OBS's recording folder and the camera
    to its own isolated file simultaneously. Call setup_scene first."""
    obs().start_recording()
    return {"recording": True}


@mcp.tool()
def stop_recording() -> dict[str, Any]:
    """Stop recording and return this take's file pair: `screen` (clean screen
    recording) and `camera` (isolated camera file). Feed these to compose_camera."""
    return obs().stop_recording()


@mcp.tool()
def recording_status() -> dict[str, Any]:
    """Current recording state: active, timecode, and bytes written."""
    return obs().recording_status()


@mcp.tool()
def compose_camera(
    screen_path: str,
    camera_path: str,
    keyframes: list[dict[str, Any]],
    output_path: str = "",
    remove_background: bool = False,
    max_duration: float | None = None,
) -> dict[str, Any]:
    """Composite the isolated camera over the clean screen recording, ANIMATING the
    camera's position and scale along a keyframe timeline, and write a final mp4
    (ready for short-form-editor's create_project).

    screen_path / camera_path: the pair returned by stop_recording.

    keyframes: ordered list describing the camera over time. Each item has `t`
    (seconds) plus EITHER a `preset` shorthand or explicit `scale`+`anchor`/`pos`:
      - preset: "fullscreen", "top-left", "top-right", "bottom-left",
        "bottom-right", "center", "pip".
      - scale: fraction of frame height the camera fills (1.0 = fullscreen, ~0.28 PiP).
      - anchor: "center"|"top-left"|"top-right"|"bottom-left"|"bottom-right" (margin
        from edges), or pos: [nx, ny] explicit normalized center (0..1).
      - ease (into this keyframe): "inout" (default), "linear", "in", "out".
    The camera holds the first keyframe before it and the last after it, easing
    between them. Example: [{"t":0,"preset":"fullscreen"},
    {"t":5,"preset":"bottom-right"}, {"t":12,"preset":"top-left"}].

    remove_background: AI-matte the camera so the subject floats (no rectangle).
    Defaults to the plain rectangle.
    """
    if not output_path:
        os.makedirs(config.CAMERA_DIR, exist_ok=True)
        base = os.path.splitext(os.path.basename(camera_path))[0]
        output_path = os.path.join(config.CAMERA_DIR, f"composed_{base}.mp4")
    return compositor.compose(
        screen_path, camera_path, keyframes, output_path,
        remove_background=remove_background, max_duration=max_duration,
    )
