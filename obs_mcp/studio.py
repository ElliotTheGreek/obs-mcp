"""FlowDot Studio: one unified MCP that merges the OBS capture/compositor tools
(this package) with the short-form-editor transcript-cutting engine into a single
end-to-end video editor.

Pipeline exposed by the combined toolset:
  capture (OBS)  ->  compose master (camera anim, bg-removal, overlays, blur/focus)
                 ->  create_project + transcribe (cloud word-STT)
                 ->  EDL cut + cleanup + reframe + captions + title + loudnorm
                 ->  multi-aspect export  +  verify / suggest / thumbnails

We REUSE both servers' already-registered tools/resources rather than re-declaring
them, so there is a single source of truth for each capability.
"""

from __future__ import annotations

import os

# Claude Code may launch this stdio server with a reduced environment. On Windows a
# child process missing SystemRoot/WINDIR fails Python's SSL/socket init, which surfaces
# from the OpenAI SDK as a bare "Connection error." Restore them before any HTTPS call.
if os.name == "nt":
    os.environ.setdefault("SystemRoot", r"C:\Windows")
    os.environ.setdefault("SYSTEMROOT", r"C:\Windows")
    os.environ.setdefault("WINDIR", r"C:\Windows")

# Claude Code may inject a proxy into the server's env. httpx (used by the openai SDK)
# honors it (trust_env), routing cloud STT through a proxy that doesn't work here, while
# raw sockets bypass it and go direct -- surfacing as a bare "Connection error." This
# machine has direct egress, so strip proxy vars before any HTTPS client is built.
_PROXY_KEYS = ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy")
_ORIGINAL_PROXY_ENV = {k: os.environ[k] for k in list(os.environ)
                       if k.lower() in _PROXY_KEYS}
for _k in list(os.environ):
    if _k.lower() in _PROXY_KEYS:
        os.environ.pop(_k, None)

# An EMPTY OPENAI_BASE_URL is read by the openai SDK as the base URL and breaks every
# request (scheme-less URL -> "Connection error."). Drop it so the SDK uses its default.
if not os.environ.get("OPENAI_BASE_URL", "").strip():
    os.environ.pop("OPENAI_BASE_URL", None)

from mcp.server.fastmcp import FastMCP

from . import studio_learn
from .server import mcp as _obs
from short_form_editor_mcp.server import mcp as _sfe

mcp = FastMCP(
    "flowdot-studio",
    instructions=(
        "Unified video editor: record (OBS) -> compose a master (camera animation, "
        "background removal, pointer graphics, blur/focus) -> cut to shorts by "
        "transcript (cloud STT, EDL, captions, subject-tracked reframe, multi-aspect). "
        "READ learn://studio-overview FIRST for the full map and tool order. Then "
        "learn://capture and learn://compose for the OBS/compositor half, and "
        "learn://workflow + learn://cutting + learn://captions + learn://reframe + "
        "learn://gotchas for the transcript-editing half. These are MCP resources — "
        "fetch them with the resource-read mechanism, they are not tools."
    ),
)


def _merge(dst: FastMCP, src: FastMCP) -> None:
    dst._tool_manager._tools.update(src._tool_manager._tools)
    for mgr in ("_resource_manager",):
        s = getattr(src, mgr, None)
        d = getattr(dst, mgr, None)
        if s is None or d is None:
            continue
        for store in ("_resources", "_templates"):
            sv, dv = getattr(s, store, None), getattr(d, store, None)
            if isinstance(sv, dict) and isinstance(dv, dict):
                dv.update(sv)


# SFE first, then OBS (no tool-name collisions remain after grab_frame rename)
_merge(mcp, _sfe)
_merge(mcp, _obs)


# capture/compositor learn:// docs (the editor half ships its own from SFE)
@mcp.resource("learn://studio-overview")
def _learn_studio_overview() -> str:
    return studio_learn.STUDIO_OVERVIEW


@mcp.resource("learn://capture")
def _learn_capture() -> str:
    return studio_learn.CAPTURE


@mcp.resource("learn://compose")
def _learn_compose() -> str:
    return studio_learn.COMPOSE


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
