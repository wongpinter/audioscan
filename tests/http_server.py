"""A small HTTP server that serves bytes with real ``Range`` support.

Shared by the transfer tests and the end-to-end test. Server-side byte accounting
(``bytes_served``) lets tests verify traffic independently of the client's own
counters.
"""

from __future__ import annotations

import contextlib
import http.server
import sys
import threading
from collections.abc import Iterator


class ServerState:
    """Mutable server behaviour plus a request/byte log."""

    def __init__(
        self,
        payload: bytes,
        *,
        range_allowed: bool = True,
        head_allowed: bool = True,
        fail_times: int = 0,
        fail_status: int = 503,
        retry_after: str | None = None,
    ) -> None:
        self.payload = payload
        self.range_allowed = range_allowed
        self.head_allowed = head_allowed
        self.fail_times = fail_times
        self.fail_status = fail_status
        self.retry_after = retry_after
        self.requests: list[tuple[str, str | None]] = []
        self.bytes_served = 0
        self.lock = threading.Lock()

    def should_fail(self) -> bool:
        """Consume one planned failure, so retry behaviour can be tested."""
        with self.lock:
            if self.fail_times <= 0:
                return False
            self.fail_times -= 1
            return True

    def record(self, method: str, range_header: str | None) -> None:
        with self.lock:
            self.requests.append((method, range_header))

    def count_bytes(self, count: int) -> None:
        with self.lock:
            self.bytes_served += count

    @property
    def methods(self) -> list[str]:
        return [method for method, _ in self.requests]


class RangeHandler(http.server.BaseHTTPRequestHandler):
    """Serves ``state.payload``, honouring ``Range`` unless told not to."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # keep pytest output readable
        return

    def _state(self) -> ServerState:
        return self.server.state  # type: ignore[attr-defined, no-any-return]

    def _fail(self) -> None:
        state = self._state()
        self.send_response(state.fail_status)
        if state.retry_after is not None:
            self.send_header("Retry-After", state.retry_after)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:  # noqa: N802 - http.server API
        state = self._state()
        state.record("HEAD", self.headers.get("Range"))
        if state.should_fail():
            self._fail()
            return
        if not state.head_allowed:
            self.send_error(405)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(state.payload)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        state = self._state()
        payload = state.payload
        range_header = self.headers.get("Range")
        state.record("GET", range_header)

        if state.should_fail():
            self._fail()
            return

        if range_header and state.range_allowed:
            start_text, _, end_text = range_header.split("=", 1)[1].partition("-")
            start = int(start_text)
            if start >= len(payload):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(payload)}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(int(end_text) if end_text else len(payload) - 1, len(payload) - 1)
            chunk = payload[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            self.wfile.write(chunk)
            state.count_bytes(len(chunk))
            return

        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        state.count_bytes(len(payload))


class QuietHTTPServer(http.server.ThreadingHTTPServer):
    """ThreadingHTTPServer that ignores clients dropping the connection early.

    Tests deliberately make httpx close mid-body to prove the fetcher stops reading.
    """

    def handle_error(self, request: object, client_address: object) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)  # type: ignore[arg-type]


@contextlib.contextmanager
def running_server(
    payload: bytes,
    *,
    range_allowed: bool = True,
    head_allowed: bool = True,
    fail_times: int = 0,
    fail_status: int = 503,
    retry_after: str | None = None,
) -> Iterator[tuple[str, ServerState]]:
    """Serve ``payload`` on a random localhost port for the duration of the block."""
    state = ServerState(
        payload,
        range_allowed=range_allowed,
        head_allowed=head_allowed,
        fail_times=fail_times,
        fail_status=fail_status,
        retry_after=retry_after,
    )
    server = QuietHTTPServer(("127.0.0.1", 0), RangeHandler)
    server.state = state  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/book.m4b", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
