"""Minimal, reliable obs-websocket v5 client.

Why not obsws-python: its request path does a blind ``recv()`` with no
requestId correlation, so any non-matching frame (or a stale read) is returned
as if it were the reply — which made deletes appear to succeed while actually
reading the wrong frame. This client matches every reply to the requestId it
sent and skips anything else, which is correct against OBS 32 / websocket 5.7.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import websocket  # provided by websocket-client (dep of obsws-python; we keep it)


class OBSError(RuntimeError):
    pass


class WSClient:
    def __init__(self, host: str = "localhost", port: int = 4455,
                 password: str | None = None, timeout: float = 5.0):
        self._host, self._port, self._password = host, port, password
        self._timeout = timeout
        self._ws: websocket.WebSocket | None = None
        self._id = 0

    def connect(self) -> None:
        try:
            ws = websocket.WebSocket()
            ws.connect(f"ws://{self._host}:{self._port}", timeout=self._timeout)
            hello = json.loads(ws.recv())
        except Exception as e:  # noqa: BLE001
            raise OBSError(
                f"Could not reach OBS websocket at {self._host}:{self._port}. "
                "Is OBS running with Tools -> WebSocket Server Settings enabled?"
            ) from e

        identify: dict[str, Any] = {"rpcVersion": 1, "eventSubscriptions": 0}
        auth = hello["d"].get("authentication")
        if auth:
            if not self._password:
                raise OBSError("OBS websocket requires a password but none was given.")
            secret = base64.b64encode(
                hashlib.sha256((self._password + auth["salt"]).encode()).digest()
            ).decode()
            identify["authentication"] = base64.b64encode(
                hashlib.sha256((secret + auth["challenge"]).encode()).digest()
            ).decode()
        ws.send(json.dumps({"op": 1, "d": identify}))
        ident = json.loads(ws.recv())
        if ident.get("op") != 2:
            raise OBSError(
                "OBS rejected the websocket identify (wrong password?). "
                "Use Tools -> WebSocket Server Settings -> Show Connect Info to confirm."
            )
        self._ws = ws

    @property
    def ws(self) -> websocket.WebSocket:
        if self._ws is None:
            self.connect()
        assert self._ws is not None
        return self._ws

    def request(self, req_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        rid = f"r{self._id}"
        payload: dict[str, Any] = {"op": 6, "d": {"requestType": req_type, "requestId": rid}}
        if data:
            payload["d"]["requestData"] = data
        self.ws.send(json.dumps(payload))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("op") != 7:  # not a RequestResponse (e.g. an event) -> skip
                continue
            d = msg["d"]
            if d.get("requestId") != rid:  # response to some other request -> skip
                continue
            status = d["requestStatus"]
            if not status.get("result"):
                raise OBSError(
                    f"{req_type} failed [{status.get('code')}]: {status.get('comment')}"
                )
            return d.get("responseData", {}) or {}

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            finally:
                self._ws = None
