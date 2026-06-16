"""Environment configuration for obs-mcp."""

from __future__ import annotations

import os

# obs-websocket connection
OBS_WS_HOST = os.environ.get("OBS_WS_HOST", "localhost")
OBS_WS_PORT = int(os.environ.get("OBS_WS_PORT", "4455"))
OBS_WS_PASSWORD = os.environ.get("OBS_WS_PASSWORD", "") or None

# where the isolated camera files are written (the screen recording goes to OBS's
# own configured recording directory).
CAMERA_DIR = os.environ.get(
    "OBS_CAMERA_DIR", r"E:\FlowdotPlatform\obs-mcp\recordings"
)

# default scene name this server manages
SCENE_NAME = os.environ.get("OBS_SCENE_NAME", "ObsMcpRec")
