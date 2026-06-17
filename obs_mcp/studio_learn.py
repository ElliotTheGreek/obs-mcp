"""learn:// resource content for the unified FlowDot Studio server.

These cover the capture + compositor half (this package). The transcript-editor
half ships its own learn:// docs (overview, workflow, cutting, captions, reframe,
gotchas, hooks) from short_form_editor_mcp, which the studio server also exposes.
"""

STUDIO_OVERVIEW = """\
# FlowDot Studio — start here

One MCP, end to end: **record yourself, composite a master, then cut it to shorts
by transcript.** Two halves joined by one bridge.

```
RECORD (OBS)            COMPOSE (master.mp4)              EDIT (shorts)
setup_scene            compose_camera                    create_project(master)
start/stop_recording   - camera move/scale (keyframes)   transcribe (cloud word-STT)
  -> {screen, camera}  - background removal (floating)    -> read transcript.txt
                       - overlays: arrows/rings/labels    validate_edl  (cheap loop)
                       - blur region / focus spotlight    render -> reframe+captions+
                         = master.mp4  ───────bridge────►        loudnorm+multi-aspect
                                                          verify_clip / suggest_clips
```

## The canonical flow
1. **Capture** — `setup_scene(monitor, camera, mic)` (self-verifies track separation),
   then `start_recording` / `stop_recording`. stop returns the `{screen, camera}` file
   pair. → details in **learn://capture**.
2. **Compose a master** — `compose_camera(screen, camera, keyframes, ...)` animates the
   camera and (optionally) removes its background, adds pointer graphics, and blurs/
   focuses screen regions. Output is the master mp4. → details in **learn://compose**.
3. **Bridge** — `create_project(master.mp4)` registers the master with the editor.
4. **Transcribe** — `transcribe(project_id)` (cloud word-level STT by default; fast).
   Then **READ the project's transcript.txt** before designing cuts.
5. **Cut** — design an EDL (word-index segments, reorderable), check it cheaply with
   `validate_edl`, then `render`. Styling (reframe to 9:16, captions, title, loudnorm,
   multi-aspect) is set on the EDL. → **learn://workflow**, **learn://cutting**,
   **learn://captions**, **learn://reframe**.
6. **QA / variants** — `verify_clip` (re-STT diff), `suggest_clips`, `extract_thumbnail`.
7. Always skim **learn://gotchas** (editor) — and the notes below.

## Tool map
- **Capture:** list_devices, setup_scene, start_recording, stop_recording, recording_status
- **Compose:** compose_camera, list_graphics, grab_screen_frame
- **Edit:** create_project, transcribe, get_transcript, validate_edl, render, verify_clip,
  suggest_clips, preview_reframe, grab_frame, extract_thumbnail, list_caption_presets,
  list_reframe_modes, get_project, list_projects

## Environment facts (don't re-derive)
- OBS must be running with its **WebSocket server enabled** and the **Source Record**
  plugin installed (camera records to its own isolated file). Connection comes from
  OBS_WS_* env.
- **STT defaults to cloud** (STT_BACKEND=openai) for speed — no local model load. Local
  WhisperX is available as a fallback (pass backend="whisperx").
- You can compose a master OR feed any existing video with speech straight to
  create_project; the compose step is only needed when you want the camera/graphics/blur.
- All coordinate inputs (overlay `pos`, blur `pos`) are **normalized** [x/width, y/height];
  use grab_screen_frame (pre-project) or grab_frame (in-project) to read a frame and pick.
"""

CAPTURE = """\
# Capture (OBS) — track-separated recording

The point of capture is to get **two clean files**: the screen with NO camera baked in,
and the camera in its OWN isolated file, so the camera can be freely animated in post.

## Prerequisites (one-time, in OBS)
- OBS running, **Tools → WebSocket Server Settings → enabled** (port/password are wired
  via OBS_WS_* env).
- **Source Record** plugin installed (github.com/exeldro/obs-source-record) — this is what
  writes the isolated camera file. setup_scene errors clearly if it's missing.

## Tools
- **list_devices()** → `{monitors, cameras, mics}` each `{name, device_id}`. Pick by a
  name substring in the next call.
- **setup_scene(monitor, camera, mic, mic_track=1)** — builds the scene and **proves it
  records before returning**:
  - full-screen capture of `monitor` = the clean screen recording;
  - `camera` enabled but **off-canvas** (so it streams frames yet never appears on screen),
    recorded to its own file via Source Record;
  - `mic` on the main audio track (so the screen file carries the spoken audio).
  - Idempotent: re-calling with the same devices does NOT reopen the webcam (a USB cam
    exhausts if cycled too fast). It runs a throwaway test recording and retries until the
    isolated camera file is confirmed. Returns `ready: true`.
- **start_recording()** / **stop_recording()** — records screen + camera together;
  `stop_recording` returns this take's `{screen, camera}` pair (feed both to compose_camera).
- **recording_status()** — active / timecode / bytes.

## Notes
- Accept a name substring ("Primary", "NexiGo", "JOUNIVO") or an exact device_id.
- OBS only does clean capture — every visual decision (camera position, graphics, blur)
  is deferred to compose_camera, so nothing is baked in and everything stays re-editable.
- Server commands / deployments are the user's to run — never expose or restart anything
  yourself; ask the user.
"""

COMPOSE = """\
# Compose — the master video (camera + graphics + blur)

`compose_camera(screen_path, camera_path, keyframes, output_path="",
remove_background=False, max_duration=None, overlays=None, blurs=None)` takes the
`{screen, camera}` pair and writes a finished master mp4. Non-destructive: originals are
never modified; each call writes a new file, so you can re-render freely.

## Camera animation — `keyframes`
Ordered list; each item has `t` (seconds) plus either a `preset` or explicit fields:
- preset: `fullscreen`, `top-left`, `top-right`, `bottom-left`, `bottom-right`, `center`,
  `pip`.
- `scale`: fraction of frame HEIGHT the camera fills (1.0 = fullscreen, ~0.28 PiP).
- `anchor`: corner/center, or `pos`: [nx, ny] explicit normalized center.
- `ease` into the keyframe: `inout` (default), `linear`, `in`, `out`.
The camera holds the first keyframe before it and the last after it.
Example: `[{"t":0,"preset":"fullscreen"},{"t":5,"preset":"bottom-right"}]`.

## Background removal — `remove_background`
AI-mattes the camera so the subject floats over the screen (no rectangle). CPU-bound
(~10x realtime); fine for short masters.

## Overlays — `overlays` (pointer graphics, ON TOP of the camera)
Each overlay's source is ONE of:
- built-in: `{"kind":"arrow"|"ring"|"box"|"label", ...}` (see list_graphics). The arrow's
  TIP is its anchor; ring/box/label anchor at center.
- custom: `{"svg":"<inline svg>"}`  ·  pre-rendered: `{"image":"path-to-rgba.png"}`
  (e.g. a Gemini graphic: image-tools gemini_generate_image on a flat bg → remove_background
  → pass the transparent PNG here).
Animation: `anchor` [ax,ay] (point placed on pos), `keyframes`
`[{t, pos:[nx,ny], scale(=width as frac of frame WIDTH), ease}]`, `t_in`/`t_out` (appear/
disappear s), `fade`, `opacity`. To point at a screen element, call
**grab_screen_frame(video, t)**, READ the PNG, and convert the target pixel to normalized
`[x/width, y/height]`.

## Blur — `blurs` (applied to the screen, UNDER the camera)
One operation, "blur where the mask is 1", chosen by `invert`:
- `invert:false` = **redact**: blur INSIDE the shape (hide a key/name/panel).
- `invert:true` = **focus/spotlight**: blur everything OUTSIDE the shape (optionally `dim`).
Fields: `shape` (`rect`|`circle`|`svg`), `strength` (blur px), `feather` (soft edge px),
`aspect` (w/h, for wide rects), `dim` (0..1 darken), `keyframes`
`[{t, pos:[nx,ny] = shape CENTER, scale = frac of frame WIDTH, ease}]`, `t_in`/`t_out`/`fade`.

## After composing
Pass the master to `create_project(master_path)` to start transcript editing. Or skip
compose entirely and create_project on any video that already has speech.

## Notes
- Output resolution follows OBS's Output setting (e.g. 720p) — raise it in OBS for 1080p.
- `max_duration` caps the render length (handy for quick reviews).
"""
