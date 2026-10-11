"""Keeps track of the applications HealOps is allowed to monitor and heal."""

from dataclasses import dataclass

from healops.capabilities import Capabilities
from healops.events import _require_text
from healops.exceptions import (
    ApplicationAlreadyRegisteredError,
    UnknownApplicationError,
)


@dataclass(frozen=True, slots=True)
class RegisteredApplication:
    """An application name together with its adapter and what it can do."""

    name: str
    adapter: object
    capabilities: Capabilities


class ApplicationRegistry:
    """A plain object (no global state): create one per HealOps agent."""

    def __init__(self) -> None:
        self._applications: dict[str, RegisteredApplication] = {}

    def register(self, name: str, adapter: object) -> RegisteredApplication:
        """Register an adapter under `name`.

        Capabilities are discovered first, so a broken adapter is rejected
        here and never stored.
        """
        _require_text(name, "name")
        if name in self._applications:
            raise ApplicationAlreadyRegisteredError(
                f"application {name!r} is already registered"
            )
        capabilities = Capabilities.discover(adapter)
        application = RegisteredApplication(
            name=name, adapter=adapter, capabilities=capabilities
        )
        self._applications[name] = application
        return application

    def get(self, name: str) -> RegisteredApplication:
        try:
            return self._applications[name]
        except KeyError:
            raise UnknownApplicationError(
                f"application {name!r} is not registered"
            ) from None

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._applications))

    def __contains__(self, name: object) -> bool:
        return name in self._applications

    def __len__(self) -> int:
        return len(self._applications)