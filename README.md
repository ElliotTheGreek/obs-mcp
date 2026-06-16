# obs-mcp

Local MCP server that drives **OBS Studio** for **track-separated recording** and
**post-recording animated-camera compositing**, then hands a finished mp4 to
`short-form-editor-mcp`.

You record yourself with OBS (screen + webcam), and afterward describe how the
camera box should move — *"box top-left, then fullscreen at 0:30, then shrink to the
bottom-right corner"* — and the server renders exactly that. This works because the
camera is recorded to its **own isolated file** (never baked into the screen), so it
can be freely repositioned and scaled after the fact.

## How it works

```
setup_scene  ->  start/stop_recording  ->  compose_camera  ->  short-form-editor
 (verified        (screen.mp4 +              (animated camera     (create_project
  separation)      camera.mkv pair)           over screen -> mp4)   on the final mp4)
```

1. **`setup_scene(monitor, camera, mic)`** — builds an OBS scene: full-screen capture
   of the chosen monitor (the clean screen recording) + the webcam recorded to its
   own file via the **Source Record** plugin (off-canvas, so it never appears in the
   screen file) + the mic on the main audio track. It then runs a throwaway test
   recording to **prove** the isolated camera file records before returning. Idempotent
   — re-calling with the same devices never reopens the webcam.
2. **`start_recording()` / `stop_recording()`** — records screen and camera
   simultaneously; `stop_recording` returns this take's `{screen, camera}` file pair.
3. **`compose_camera(screen, camera, keyframes, ...)`** — composites the camera over
   the screen, animating position + scale along the keyframe timeline, and writes a
   final mp4. Optional `remove_background` mattes the subject so it floats.
4. Feed the final mp4 to `short-form-editor`'s `create_project`.

## Keyframe timeline

Each keyframe has a time `t` (seconds) plus either a `preset` or explicit
`scale` + `anchor`/`pos`:

```json
[
  {"t": 0,  "preset": "fullscreen"},
  {"t": 5,  "preset": "bottom-right"},
  {"t": 12, "scale": 0.28, "anchor": "top-left", "ease": "inout"}
]
```

- **preset**: `fullscreen`, `top-left`, `top-right`, `bottom-left`, `bottom-right`,
  `center`, `pip`.
- **scale**: fraction of frame height the camera fills (`1.0` = fullscreen, `~0.28`
  PiP).
- **anchor**: corner/center (margin from edges), or **pos** `[nx, ny]` explicit
  normalized center.
- **ease** (into the keyframe): `inout` (default), `linear`, `in`, `out`.

The camera holds the first keyframe before it and the last after it, easing between.

## One-time setup (in OBS)

1. **Enable the WebSocket server**: *Tools → WebSocket Server Settings* → enable;
   note the port (default `4455`) and password.
2. **Install Source Record**: https://github.com/exeldro/obs-source-record/releases,
   then restart OBS. (Required for the separate camera file.)

## Install

```powershell
py -3.11 -m venv E:\FlowdotPlatform\obs-mcp\.venv
$py = "E:\FlowdotPlatform\obs-mcp\.venv\Scripts\python.exe"
& $py -m pip install -e E:\FlowdotPlatform\obs-mcp          # OBS control + compositor
& $py -m pip install -e E:\FlowdotPlatform\obs-mcp[matting] # (optional) background removal
```

Requires: OBS 28+ (built-in websocket), the Source Record plugin, ffmpeg on PATH.

## Register in `.mcp.json`

```json
"obs": {
  "command": "E:\\FlowdotPlatform\\obs-mcp\\.venv\\Scripts\\python.exe",
  "args": ["-m", "obs_mcp"],
  "env": {
    "OBS_WS_HOST": "localhost",
    "OBS_WS_PORT": "4455",
    "OBS_WS_PASSWORD": "<your obs websocket password>",
    "OBS_CAMERA_DIR": "E:\\FlowdotPlatform\\obs-mcp\\recordings"
  }
}
```

## Config (env vars)

| var | default | meaning |
|---|---|---|
| `OBS_WS_HOST` | `localhost` | OBS websocket host |
| `OBS_WS_PORT` | `4455` | OBS websocket port |
| `OBS_WS_PASSWORD` | — | OBS websocket password |
| `OBS_CAMERA_DIR` | `E:\FlowdotPlatform\obs-mcp\recordings` | where isolated camera files + composites are written |
| `OBS_SCENE_NAME` | `ObsMcpRec` | the scene this server manages |

## Hard-won implementation notes

- **Custom correlated websocket client** (`ws.py`): obs-websocket replies are matched
  to their `requestId`; a blind `recv()` (as in obsws-python) intermittently returns
  the wrong frame and makes deletes look like no-ops.
- **Async device teardown**: removing an active capture source returns success
  ~100–200 ms before the device is actually released; `_ensure_absent` polls until
  the source is gone before recreating it.
- **Source Record filter must set `rate_control: "CBR"`** (uppercase) and
  `record_mode: 3` ("Recording"); the plugin default lowercase `cbr` silently fails
  nvenc init and writes no file.
- **Camera is enabled but off-canvas** so it streams frames (a hidden dshow source is
  inactive) without appearing in the screen recording.
- **Idempotent `build_scene`**: re-opening a USB webcam too rapidly triggers DShow
  `0x800705AA` ("Insufficient system resources"); the camera is opened at most once
  per device choice.
- **`setup_scene` self-verifies** with a throwaway test recording before declaring the
  separation ready.
