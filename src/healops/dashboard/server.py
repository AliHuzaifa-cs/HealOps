"""The dashboard server: a read-mostly web page over the HealOps state file and audit log.

Standard library only. By default it listens on this computer only (127.0.0.1) and refuses
requests whose Host header is not a local name, which blocks DNS-rebinding tricks. It can
answer approvals only when started with `--allow-actions`; the answer is written to the state
file exactly like `healops approve`, and the running HealOps carries it out.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from healops.audit import AuditEvent, read_events
from healops.dashboard import views
from healops.exceptions import AuditReadError, HealOpsError
from healops.store import APPROVE, DECISION_ACTIONS, MAX_REASON_LENGTH, StateStore

log = logging.getLogger("healops.dashboard")

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY_BYTES = 8 * 1024
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})

# Fixed on purpose: on Windows the registry can map .js to text/plain, which browsers refuse.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; font-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
}


@dataclass(slots=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


def _json(status: int, data: object) -> Response:
    body = json.dumps(data, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return Response(
        status,
        {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"},
        body,
    )


def _error(status: int, message: str) -> Response:
    return _json(status, {"error": message})


def _host_name(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[: end + 1] if end != -1 else host
    return host.rsplit(":", 1)[0] if ":" in host else host


class DashboardApp:
    """Answers requests. Has no sockets, so tests can call `dispatch` directly."""

    def __init__(
        self,
        db_path: str | Path,
        audit_path: str | Path | None = None,
        settings: views.Settings | None = None,
        static_dir: Path | None = None,
        extra_hosts: frozenset[str] = frozenset(),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings if settings is not None else views.Settings()
        self._store = StateStore(db_path)
        self._audit_path = Path(audit_path) if audit_path else None
        self._static = static_dir if static_dir is not None else STATIC_DIR
        self._hosts = LOCAL_HOSTS | {h.lower() for h in extra_hosts}
        self._clock = clock
        self._audit_lock = threading.Lock()
        self._audit_key: tuple[int, int] | None = None
        self._audit_events: list[AuditEvent] = []
        self._audit_ok = True

    def close(self) -> None:
        self._store.close()

    # ----- reading the audit log (re-read only when the file changed) -----

    def _events(self) -> tuple[list[AuditEvent], bool]:
        path = self._audit_path
        if path is None:
            return [], True
        with self._audit_lock:
            try:
                stat = path.stat()
            except OSError:
                self._audit_key, self._audit_events, self._audit_ok = None, [], True
                return [], True
            key = (stat.st_mtime_ns, stat.st_size)
            if key != self._audit_key:
                try:
                    self._audit_events = read_events(path)
                    self._audit_ok = True
                except AuditReadError as error:
                    log.warning("audit log not read: %s", error)
                    self._audit_events, self._audit_ok = [], False
                self._audit_key = key
            return self._audit_events, self._audit_ok

    def _snapshot(self) -> views.Snapshot:
        events, ok = self._events()
        return views.take_snapshot(self._store, events, self.settings, self._clock(), ok)

    # ----- routing -----

    def dispatch(
        self, method: str, target: str, headers: Mapping[str, str], body: bytes = b""
    ) -> Response:
        lowered = {k.lower(): v for k, v in headers.items()}
        host = _host_name(lowered.get("host", ""))
        if host not in self._hosts:
            return _error(403, "this host name is not allowed")

        parts = urlsplit(target)
        path = unquote(parts.path)
        query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        try:
            if path.startswith("/api/"):
                return self._api(method, path, query, lowered, body)
            if method not in ("GET", "HEAD"):
                return _error(405, "method not allowed")
            return self._static_file(path)
        except HealOpsError as error:
            log.warning("request failed: %s", error)
            return _error(500, "HealOps could not read its saved state")
        except Exception:  # noqa: BLE001 - never leak internals to the browser
            log.exception("unexpected error")
            return _error(500, "unexpected error")

    def _api(
        self, method: str, path: str, query: dict[str, str], headers: dict[str, str], body: bytes
    ) -> Response:
        if method == "POST":
            return self._post(path, headers, body)
        if method not in ("GET", "HEAD"):
            return _error(405, "method not allowed")

        snap = self._snapshot()
        if path == "/api/shell":
            return _json(200, views.shell(snap))
        if path == "/api/overview":
            return _json(200, {"shell": views.shell(snap), **views.overview(snap, query.get("range"))})
        if path == "/api/incidents":
            return _json(
                200, {"shell": views.shell(snap), **views.incidents_page(snap, query.get("range"))}
            )
        if path == "/api/incidents.csv":
            text = views.incidents_csv(snap, query.get("range"))
            return Response(
                200,
                {
                    "Content-Type": "text/csv; charset=utf-8",
                    "Content-Disposition": 'attachment; filename="healops-incidents.csv"',
                    "Cache-Control": "no-store",
                },
                text.encode("utf-8"),
            )
        if path.startswith("/api/incidents/"):
            incident_id = path[len("/api/incidents/") :]
            detail = views.incident_detail(snap, incident_id)
            if detail is None:
                return _error(404, "no such incident")
            return _json(200, {"shell": views.shell(snap), **detail})
        return _error(404, "not found")

    def _post(self, path: str, headers: dict[str, str], body: bytes) -> Response:
        prefix, suffix = "/api/incidents/", "/decision"
        if not (path.startswith(prefix) and path.endswith(suffix)):
            return _error(404, "not found")
        if not self.settings.actions_enabled:
            return _error(
                403, "answers from the dashboard are off; start it with --allow-actions"
            )
        if not headers.get("content-type", "").lower().startswith("application/json"):
            return _error(415, "send JSON")
        origin = headers.get("origin")
        if origin and _host_name(urlsplit(origin).netloc) not in self._hosts:
            return _error(403, "request came from another site")
        if len(body) > MAX_BODY_BYTES:
            return _error(413, "request too large")
        try:
            data = json.loads(body or b"{}")
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            return _error(400, "invalid JSON")

        action = data.get("action")
        reason = data.get("reason")
        if action not in DECISION_ACTIONS:
            return _error(400, "action must be 'approve' or 'reject'")
        if reason is not None and (not isinstance(reason, str) or len(reason) > MAX_REASON_LENGTH):
            return _error(400, "reason must be short text")

        incident_id = path[len(prefix) : -len(suffix)]
        pending = next(
            (p for p in self._store.pending() if p.incident.incident_id == incident_id), None
        )
        if pending is None:
            return _error(409, "nothing is waiting for approval for this incident")
        try:
            decision_id = self._store.add_decision(
                incident_id,
                pending.proposal.proposal_id,
                action,
                self.settings.user,
                reason or None,
                now=self._clock(),
            )
        except ValueError as error:
            return _error(400, str(error))
        beat = self._store.last_beat()
        running = beat is not None and (self._clock() - beat).total_seconds() <= (
            views.HEARTBEAT_FRESH_SECONDS
        )
        verb = "approval" if action == APPROVE else "rejection"
        message = f"Your {verb} is saved. HealOps applies it on its next check."
        if self.settings.demo:
            message = f"Demo: your {verb} is saved, but no real HealOps is running here to apply it."
            running = False
        elif not running:
            message += " HealOps does not look like it is running, so nothing will happen yet."
        return _json(200, {"decision_id": decision_id, "message": message, "agent_running": running})

    # ----- the built web page -----

    def _static_file(self, path: str) -> Response:
        root = self._static.resolve()
        relative = path.lstrip("/") or "index.html"
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            candidate = root / "index.html"
            if not candidate.is_file():
                return Response(
                    503,
                    {"Content-Type": "text/plain; charset=utf-8"},
                    b"The dashboard page is not built in this install.",
                )
        data = candidate.read_bytes()
        kind = CONTENT_TYPES.get(candidate.suffix.lower()) or (
            mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        )
        cache = "no-cache" if candidate.name == "index.html" else "public, max-age=31536000, immutable"
        return Response(200, {"Content-Type": kind, "Cache-Control": cache}, data)


def _handler(app: DashboardApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "HealOpsDashboard"
        sys_version = ""

        def _serve(self, send_body: bool) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(length, MAX_BODY_BYTES + 1)) if length else b""
            response = app.dispatch(self.command, self.path, dict(self.headers), body)
            self.send_response(response.status)
            for name, value in {**SECURITY_HEADERS, **response.headers}.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            if send_body:
                self.wfile.write(response.body)

        def do_GET(self) -> None:  # noqa: N802
            self._serve(True)

        def do_HEAD(self) -> None:  # noqa: N802
            self._serve(False)

        def do_POST(self) -> None:  # noqa: N802
            self._serve(True)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            log.debug("%s - %s", self.address_string(), format % args)

    return Handler


def serve(app: DashboardApp, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """Create the HTTP server (the caller runs `serve_forever`)."""
    server = ThreadingHTTPServer((host, port), _handler(app))
    server.daemon_threads = True
    return server
