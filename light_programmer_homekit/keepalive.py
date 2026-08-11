"""Keep-alive HTTP with a small connection pool (stdlib only).

Why this exists instead of `urllib.request.urlopen`:

`urlopen` has no connection reuse — every call is a fresh TCP connection, and
every finished connection sits in TIME_WAIT for 2*MSL afterwards. On 2026-08-11
the home server wedged: 19,453 sockets stuck in TIME_WAIT pinned all 16,384
ephemeral ports (49152-65535) and every service on the box began failing with
`[Errno 49] Can't assign requested address`. The proximate cause was a wedged
kernel TIME_WAIT reaper, not the request rate — a healthy reaper absorbs ~1-2
conn/s without trouble. But a poller that burns a connection per request gives
the machine nothing to survive on when reclamation misbehaves. Holding
connections open keeps the steady-state socket count near zero instead of
manufacturing thousands of TIME_WAIT entries an hour.

Kept deliberately small and dependency-free, and duplicated verbatim into the
sibling repos that poll matter_webcontrol, since they deploy independently with
their own venvs and share no common package. If you fix a bug here, fix it in
matter-homekit-ac, light-programmer-homekit and light_programmer together.
"""
from __future__ import annotations

import http.client
import threading
from typing import Optional
from urllib.parse import urlsplit


class HTTPError(OSError):
    """A non-2xx response.

    Raised so callers keep the failure semantics `urlopen` gave them — it also
    raised on 4xx/5xx, and every caller here catches broad `Exception` and logs.
    """

    def __init__(self, status: int, reason: str, path: str, body: bytes = b""):
        super().__init__(f"HTTP {status} {reason} for {path}")
        self.status = status
        self.reason = reason
        self.body = body


class KeepAlivePool:
    """A small pool of reusable connections to ONE origin.

    A pool rather than a single shared connection because callers hit this from
    several threads at once; one connection behind a lock would serialize every
    poll behind the slowest in-flight request.
    """

    def __init__(self, base_url: str, timeout: float = 5.0, max_idle: int = 4):
        parts = urlsplit(base_url if "://" in base_url else f"http://{base_url}")
        self._https = parts.scheme == "https"
        self._host = parts.hostname or "127.0.0.1"
        self._port = parts.port or (443 if self._https else 80)
        self._prefix = parts.path.rstrip("/")
        self._timeout = timeout
        self._max_idle = max_idle
        self._idle: list = []
        self._lock = threading.Lock()

    def _new(self) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
        return cls(self._host, self._port, timeout=self._timeout)

    def _checkout(self):
        """Returns (conn, reused). `reused` decides whether a failure is worth
        retrying, so it must reflect where the connection actually came from."""
        with self._lock:
            if self._idle:
                return self._idle.pop(), True
        return self._new(), False

    def _checkin(self, conn) -> None:
        with self._lock:
            if len(self._idle) < self._max_idle:
                self._idle.append(conn)
                return
        conn.close()

    def request(self, method: str, path: str, body: Optional[bytes] = None,
                headers: Optional[dict] = None):
        """Perform one request; returns (status, reason, body_bytes).

        Retries exactly once, and only on a connection that came from the pool.
        That single case — the server having quietly dropped an idle keep-alive
        connection — is the one where the request provably never reached the
        application, so replaying it cannot double-apply. A connection we just
        opened failing is a real error and propagates untouched; retrying that
        would risk running a non-idempotent request (e.g. /api/toggle) twice.
        """
        last_exc: Optional[BaseException] = None
        for attempt in (0, 1):
            conn, reused = self._checkout()
            try:
                conn.request(method, self._prefix + path, body=body,
                             headers=headers or {})
                resp = conn.getresponse()
                status, reason = resp.status, resp.reason
                # Draining is mandatory: an unread body leaves the connection
                # mid-message and would corrupt whatever request reuses it.
                data = resp.read()
                if resp.will_close:
                    conn.close()
                else:
                    self._checkin(conn)
                return status, reason, data
            except (OSError, http.client.HTTPException) as exc:
                conn.close()
                last_exc = exc
                if reused and attempt == 0:
                    continue
                raise
        raise last_exc  # pragma: no cover - loop always returns or raises

    def close(self) -> None:
        with self._lock:
            conns, self._idle = self._idle, []
        for conn in conns:
            conn.close()
