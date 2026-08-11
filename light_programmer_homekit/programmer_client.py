"""Thin HTTP client for light-programmer's /lights endpoint."""
from __future__ import annotations

import http.client
import json
import logging

from .keepalive import HTTPError, KeepAlivePool


class ProgrammerClient:
    """Polls light-programmer's mode HTTP server over kept-alive connections.

    `get_lights` runs every few seconds for the lifetime of the bridge. It used
    to open a fresh TCP connection each time; see keepalive.py for why that was
    worth removing.

    Keep-alive only actually engages against light_programmer >= 0.23.0, whose
    mode server speaks HTTP/1.1. Against an older one the server marks each
    response `Connection: close`, the pool honours that and closes, and
    behaviour is exactly what it was before — so this is safe to deploy first.
    """

    def __init__(self, base_url: str, timeout: float = 5.0,
                 api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key
        self._pool = KeepAlivePool(self.base_url, timeout=timeout)

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        status, reason, body = self._pool.request(method, path, data, headers)
        if status >= 400:
            raise HTTPError(status, reason, path, body)
        return json.loads(body) if body else {}

    def close(self) -> None:
        self._pool.close()

    def get_lights(self):
        """Per-light status list ``[{id, name, connected}, …]`` from /lights.

        Returns ``None`` when light-programmer is unreachable, so the caller can
        flip the system sensor to "disconnected" and freeze the per-light ones
        rather than reporting stale state.
        """
        try:
            data = self._request("GET", "/lights")
        except (OSError, TimeoutError, http.client.HTTPException,
                json.JSONDecodeError) as e:
            # http.client.HTTPException is in here because the pool re-raises
            # the underlying protocol error: most stale-connection failures are
            # OSErrors (RemoteDisconnected is one), but BadStatusLine and
            # IncompleteRead are not, and dropping them would crash the poll.
            logging.warning(f"get_lights failed: {e}")
            return None
        if not isinstance(data, dict):
            return None
        return data.get("lights", [])
