"""OBS control for track-separated recording, on a reliable correlated websocket
client (see ws.py for why obsws-python was dropped).

Protocol facts grounded against this machine (OBS 32.1.2 / websocket 5.7.3):
- Input kinds (Windows): monitor capture = ``monitor_capture``, camera =
  ``dshow_input``, mic = ``wasapi_input_capture``.
- Device lists are list-property items on a live input, read via
  ``GetInputPropertiesListPropertyItems``.
- Separate clean camera file = Source Record plugin, filter kind
  ``source_record_filter`` (default record mode "Output Active" = records exactly
  while the main OBS recording runs).
- The camera source must be ENABLED (a hidden dshow source produces no frames) but
  moved OFF-CANVAS so it never lands in the screen recording; Source Record taps the
  source's native frames regardless of scene placement.
"""

from __future__ import annotations

import dataclasses
import glob
import os
import time
from typing import Any

from .ws import OBSError, WSClient

KIND_MONITOR = "monitor_capture"
KIND_CAMERA = "dshow_input"
KIND_MIC = "wasapi_input_capture"

DEVICE_PROP = {
    KIND_MONITOR: "monitor_id",
    KIND_CAMERA: "video_device_id",
    KIND_MIC: "device_id",
}

SOURCE_RECORD_FILTER_KIND = "source_record_filter"


@dataclasses.dataclass
class Device:
    name: str
    device_id: str


class OBSClient:
    # a freshly created Source Record filter (plus a just-recreated camera device)
    # needs a moment to wire its recording-start hook; starting the recording before
    # then silently misses it and no camera file is produced. 3s was flaky at the
    # threshold; 6s held across repeated runs.
    FILTER_SETTLE_S = 6.0

    def __init__(self, host: str = "localhost", port: int = 4455, password: str | None = None):
        self._ws = WSClient(host=host, port=port, password=password)
        self._record_ready_at = 0.0
        self._camera_dir: str | None = None
        self._cam_before: set[str] = set()

    def req(self, t: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._ws.request(t, data)

    def close(self) -> None:
        self._ws.close()

    # -- info ---------------------------------------------------------------
    def version(self) -> dict[str, Any]:
        v = self.req("GetVersion")
        return {
            "obs_version": v.get("obsVersion"),
            "websocket_version": v.get("obsWebSocketVersion"),
            "platform": v.get("platform"),
        }

    def has_source_record(self) -> bool:
        kinds = self.req("GetSourceFilterKindList").get("sourceFilterKinds", [])
        return SOURCE_RECORD_FILTER_KIND in kinds

    # -- device enumeration -------------------------------------------------
    def list_devices(self, kind: str) -> list[Device]:
        prop = DEVICE_PROP[kind]
        tmp = f"__obsmcp_probe_{kind}"
        self._ensure_absent(tmp)
        self.req("CreateInput", {
            "sceneName": self._first_scene(), "inputName": tmp,
            "inputKind": kind, "inputSettings": {}, "sceneItemEnabled": False,
        })
        try:
            items = self.req("GetInputPropertiesListPropertyItems",
                             {"inputName": tmp, "propertyName": prop}).get("propertyItems", [])
        finally:
            self._ensure_absent(tmp)
        out: list[Device] = []
        for it in items:
            val = it.get("itemValue")
            if val:
                out.append(Device(name=it.get("itemName") or val, device_id=val))
        return out

    def list_monitors(self) -> list[Device]:
        return self.list_devices(KIND_MONITOR)

    def list_cameras(self) -> list[Device]:
        return self.list_devices(KIND_CAMERA)

    def list_mics(self) -> list[Device]:
        return self.list_devices(KIND_MIC)

    # -- scene construction -------------------------------------------------
    def build_scene(self, scene_name: str, monitor_id: str, camera_id: str, mic_id: str,
                    camera_record_dir: str, mic_track: int = 1) -> dict[str, Any]:
        """Idempotent. Re-calling with the same devices does NOT tear down and reopen
        the camera -- rapid open/close of a USB webcam exhausts it (DShow 0x800705AA)
        and it stops producing frames. We only (re)create an input when it is missing
        or its device changed, so the camera opens at most once per device choice.
        """
        self._ensure_scene(scene_name)
        churned = False

        churned |= self._ensure_input(
            scene_name, "Screen", KIND_MONITOR, "monitor_id", monitor_id,
            {"monitor_id": monitor_id, "capture_cursor": True})[1]

        # camera ENABLED (so the webcam streams frames) but pushed OFF-CANVAS so it
        # never appears in the screen recording. Source Record captures its native
        # frames directly, independent of scene placement.
        cam_item, cam_created = self._ensure_input(
            scene_name, "Camera", KIND_CAMERA, "video_device_id", camera_id,
            {"video_device_id": camera_id})
        if cam_created:
            v = self.req("GetVideoSettings")
            self.req("SetSceneItemTransform", {
                "sceneName": scene_name, "sceneItemId": cam_item,
                "sceneItemTransform": {"positionX": float(v["baseWidth"]) + 50.0,
                                       "positionY": 0.0},
            })
        churned |= cam_created
        churned |= self._ensure_source_record("Camera", camera_record_dir)

        churned |= self._ensure_input(
            scene_name, "Mic", KIND_MIC, "device_id", mic_id, {"device_id": mic_id})[1]
        # ensure the mic is on `mic_track` (default 1) so it lands in OBS's main
        # recording file -> the screen .mp4 carries the spoken audio for the
        # composite + transcription downstream.
        self.req("SetInputAudioTracks", {
            "inputName": "Mic", "inputAudioTracks": {str(mic_track): True}})

        self.req("SetCurrentProgramScene", {"sceneName": scene_name})
        self._camera_dir = camera_record_dir
        # only impose the settle when something was actually (re)created; an unchanged,
        # already-armed scene records immediately.
        if churned:
            self._record_ready_at = time.monotonic() + self.FILTER_SETTLE_S
        return {
            "scene": scene_name, "screen_source": "Screen", "camera_source": "Camera",
            "mic_source": "Mic", "mic_track": mic_track,
            "camera_record_dir": camera_record_dir, "rebuilt": churned,
        }

    def _ensure_input(self, scene: str, name: str, kind: str, dev_key: str,
                      dev_val: str, settings: dict[str, Any]) -> tuple[int, bool]:
        """Create the input, or reuse it untouched if it already exists with the same
        device. Returns (sceneItemId, created). Reuse avoids reopening the device."""
        if name in self._input_names():
            cur = self.req("GetInputSettings", {"inputName": name}).get("inputSettings", {})
            if cur.get(dev_key) == dev_val:
                sid = self.req("GetSceneItemId",
                               {"sceneName": scene, "sourceName": name})["sceneItemId"]
                return sid, False
            self._ensure_absent(name)  # device changed -> one controlled reopen
        r = self.req("CreateInput", {
            "sceneName": scene, "inputName": name, "inputKind": kind,
            "inputSettings": settings, "sceneItemEnabled": True})
        return r["sceneItemId"], True

    def _ensure_source_record(self, source_name: str, record_dir: str) -> bool:
        """Ensure the Source Record filter exists. Returns True if newly created (the
        new filter needs a settle before it will arm on recording start)."""
        if not self.has_source_record():
            raise OBSError(
                "Source Record plugin is not loaded in OBS. Install it from "
                "https://github.com/exeldro/obs-source-record/releases and restart OBS."
            )
        settings = {
            # record_mode 3 = "Recording": camera file starts/stops with the main
            # OBS recording (0 = "None" records nothing). rate_control MUST be
            # uppercase "CBR" -- the plugin default "cbr" silently fails nvenc init.
            "record_mode": 3, "path": record_dir,
            "filename_formatting": "camera_%CCYY-%MM-%DD_%hh-%mm-%ss",
            "rec_format": "mkv", "rate_control": "CBR", "scale_type": 3,
            # UNCAP the camera file: the plugin defaults to splitting at 900s/2048MB,
            # which would chop a 30-40 min take. 0 = no limit.
            "max_time_sec": 0, "max_size_mb": 0,
        }
        existing = self.req("GetSourceFilterList", {"sourceName": source_name}).get("filters", [])
        if any(f["filterName"] == "ObsMcpSourceRecord" for f in existing):
            self.req("SetSourceFilterSettings", {
                "sourceName": source_name, "filterName": "ObsMcpSourceRecord",
                "filterSettings": settings})
            return False
        self.req("CreateSourceFilter", {
            "sourceName": source_name, "filterName": "ObsMcpSourceRecord",
            "filterKind": SOURCE_RECORD_FILTER_KIND, "filterSettings": settings})
        return True

    # -- recording ----------------------------------------------------------
    def start_recording(self) -> None:
        # block until the Source Record filter has settled, so the camera file is
        # never silently dropped by a build->record race
        wait = self._record_ready_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        # snapshot camera dir so stop_recording can identify THIS take's camera file
        if self._camera_dir:
            self._cam_before = set(glob.glob(os.path.join(self._camera_dir, "camera_*")))
        self.req("StartRecord")

    def verify_camera_capture(self, camera_record_dir: str, attempts: int = 5) -> bool:
        """Prove the isolated camera file actually records before the user relies on
        it. Source Record auto-start on a freshly built scene is intermittently racy,
        so we do throwaway test recordings (deleting both the test screen file and
        test camera file) until one produces a camera file. Returns True once proven.
        """
        main_dir = self.record_directory()
        for _ in range(attempts):
            cam_before = set(glob.glob(os.path.join(camera_record_dir, "camera_*")))
            main_before = set(glob.glob(os.path.join(main_dir, "*")))
            self.start_recording()
            time.sleep(2.0)
            self.req("StopRecord")
            time.sleep(1.5)
            cam_new = set(glob.glob(os.path.join(camera_record_dir, "camera_*"))) - cam_before
            main_new = set(glob.glob(os.path.join(main_dir, "*"))) - main_before
            for f in cam_new | main_new:  # remove throwaway test artifacts
                try:
                    os.remove(f)
                except OSError:
                    pass
            if cam_new:
                return True
            time.sleep(2.0)  # let the device/filter settle further, then retry
        return False

    def stop_recording(self, settle: float = 1.5) -> dict[str, Any]:
        """Stop and return this take's file pair: the screen recording (OBS main
        output) and the isolated camera file produced during the same window."""
        screen = self.req("StopRecord").get("outputPath")
        camera = None
        if self._camera_dir:
            time.sleep(settle)  # let Source Record finalize the mkv
            new = set(glob.glob(os.path.join(self._camera_dir, "camera_*"))) - self._cam_before
            if new:
                camera = max(new, key=os.path.getmtime)
        return {"screen": screen, "camera": camera}

    def recording_status(self) -> dict[str, Any]:
        s = self.req("GetRecordStatus")
        return {
            "active": s.get("outputActive"),
            "timecode": s.get("outputTimecode"),
            "bytes": s.get("outputBytes"),
        }

    def record_directory(self) -> str:
        return self.req("GetRecordDirectory")["recordDirectory"]

    # -- helpers ------------------------------------------------------------
    def _scene_names(self) -> list[str]:
        return [s["sceneName"] for s in self.req("GetSceneList").get("scenes", [])]

    def _input_names(self) -> list[str]:
        return [i["inputName"] for i in self.req("GetInputList").get("inputs", [])]

    def _first_scene(self) -> str:
        names = self._scene_names()
        if names:
            return names[0]
        self.req("CreateScene", {"sceneName": "Scene"})
        return "Scene"

    def _ensure_scene(self, name: str) -> None:
        if name not in self._scene_names():
            self.req("CreateScene", {"sceneName": name})

    def _ensure_absent(self, input_name: str, timeout: float = 5.0) -> None:
        if input_name not in self._input_names():
            return
        self.req("RemoveInput", {"inputName": input_name})
        # active capture devices (camera/mic/display) tear down asynchronously
        # (~100-200ms measured); wait for the source to actually disappear before
        # callers recreate a same-named input, else CreateInput hits 601.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if input_name not in self._input_names():
                return
            time.sleep(0.1)
        raise OBSError(f"Input {input_name!r} did not tear down within {timeout}s.")
