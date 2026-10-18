"""What a device does in answer to something: bytes to put on the wire, each with a delay in front of it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Emission:
    delay: float  # seconds after the previous part ended (or after the request, for the first part)
    data: bytes


@dataclass
class Exchange:
    request: bytes
    emissions: list[Emission]
    function: int | None = None
    faultable: bool = True  # false for echo, greeting and prompts: those are not "answers" that can go wrong
    notes: list[str] = field(default_factory=list)


class Behaviour(Protocol):
    """What a device kind does. Pure: no file descriptors, no clock; the engine supplies bytes and time."""

    def start(self, now: float) -> list[Exchange]: ...

    def feed(self, data: bytes, now: float) -> list[Exchange]: ...

    def tick(self, now: float) -> list[Exchange]: ...

    def deadline(self) -> float | None: ...

    def drain_events(self) -> list[str]: ...

    def protocol_fault(self, spec: object, exchange: Exchange) -> Exchange: ...
