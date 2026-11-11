"""Restart a docker compose service through the Docker engine, with an allow-list.

HealOps may restart a *named service* and nothing else:

* only the services listed in the configuration (``docker.restartable_services``), found by
  their docker compose service label;
* only the engine's ``restart`` call (it also starts a container that was stopped);
* no shell and no ``docker`` program; the only other call is the lookup of the container.

It needs the Docker socket inside the container that runs HealOps (``/var/run/docker.sock``).
Whoever can use that socket controls Docker on the computer, so give it only to the HealOps
container, and only on a computer you own. The allow-list is what keeps HealOps to one small job.

``DockerServiceAdapter`` wraps your adapter: restarting an allowed service goes to Docker,
every other call goes to your adapter, and ``last_action`` always names the last action run.
"""

from __future__ import annotations

import http.client
import json
import socket
from collections.abc import Callable, Iterable
from urllib.parse import quote

from healops.exceptions import AdapterError

DEFAULT_SOCKET = "/var/run/docker.sock"
SERVICE_LABEL = "com.docker.compose.service"

Request = Callable[[str, str], tuple[int, bytes]]

_ACTIONS = (
    "restart_component",
    "reload_model",
    "rollback_model",
    "request_retraining",
    "request_redeployment",
)


class DockerError(AdapterError):
    """Docker could not restart the service. The message never holds engine output."""


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # not on every Windows Python
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


def engine_request(socket_path: str = DEFAULT_SOCKET, timeout: float = 60.0) -> Request:
    def request(method: str, path: str) -> tuple[int, bytes]:
        connection = _UnixConnection(socket_path, timeout)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    return request


class DockerRestarter:
    def __init__(
        self,
        services: Iterable[str],
        socket_path: str = DEFAULT_SOCKET,
        stop_timeout_seconds: int = 10,
        request: Request | None = None,
    ) -> None:
        self.services = frozenset(services)
        self._stop_timeout = stop_timeout_seconds
        self._request = request or engine_request(socket_path)

    def can_restart(self, service: str) -> bool:
        return service in self.services

    def restart(self, service: str) -> str:
        if service not in self.services:
            raise DockerError(f"restarting {service!r} is not allowed (docker.restartable_services)")
        try:
            status, body = self._request("GET", self._lookup(service))
            if status != 200:
                raise DockerError(f"docker did not list the containers (status {status})")
            found = json.loads(body or b"[]")
            if not found:
                raise DockerError(f"no container found for the service {service!r}")
            path = f"/containers/{found[0]['Id']}/restart?t={self._stop_timeout}"
            status, _ = self._request("POST", path)
        except DockerError:
            raise
        except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as error:
            raise DockerError(f"could not reach docker ({type(error).__name__})") from None
        if status != 204:
            raise DockerError(f"docker did not restart {service!r} (status {status})")
        return f"restarted the {service} container"

    @staticmethod
    def _lookup(service: str) -> str:
        filters = json.dumps({"label": [f"{SERVICE_LABEL}={service}"]})
        return f"/containers/json?all=1&filters={quote(filters)}"


class DockerServiceAdapter:
    """Your adapter plus the ability to restart the allowed docker compose services."""

    def __init__(self, adapter: object, restarter: DockerRestarter) -> None:
        self._adapter = adapter
        self._restarter = restarter
        self.last_action: str | None = None
        # Track the last action for the other action methods too (set on the instance, so the
        # capability checks can see them).
        for name in _ACTIONS:
            target = getattr(adapter, name, None)
            if name != "restart_component" and callable(target):
                setattr(self, name, self._track(name, target))

    def _track(self, name: str, target: Callable[..., object]) -> Callable[..., object]:
        def run(*args: object, **kwargs: object) -> object:
            self.last_action = name
            return target(*args, **kwargs)

        return run

    def restart_component(self, component: str) -> str:
        self.last_action = "restart_component"
        if self._restarter.can_restart(component):
            return self._restarter.restart(component)
        inner = getattr(self._adapter, "restart_component", None)
        if inner is None:
            raise DockerError(f"cannot restart {component!r}: it is not an allowed docker service")
        return inner(component)

    def __getattr__(self, name: str) -> object:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._adapter, name)
