"""The device kinds that are not Modbus: a scripted dialogue, a stream of lines, and an echo."""

from __future__ import annotations

import re

from .exchange import Emission, Exchange
from .modbus import ModbusSlave
from .scenario import DeviceSpec

MAX_LINE = 4096
_TEMPLATE = re.compile(rb"\$\{(line|\d+)\}")


class _Plain:
    """The parts of the Behaviour interface that most kinds do not need."""

    def __init__(self) -> None:
        self._events: list[str] = []

    def start(self, now: float) -> list[Exchange]:
        return []

    def tick(self, now: float) -> list[Exchange]:
        return []

    def deadline(self) -> float | None:
        return None

    def drain_events(self) -> list[str]:
        out, self._events = self._events, []
        return out

    def protocol_fault(
        self, spec: object, exchange: Exchange
    ) -> Exchange:  # pragma: no cover - scenario check
        raise ValueError("protocol faults only exist for modbus-rtu devices")


class EchoDevice(_Plain):
    def feed(self, data: bytes, now: float) -> list[Exchange]:
        return [Exchange(request=data, emissions=[Emission(0.0, data)])]


class StreamDevice(_Plain):
    def __init__(self, spec: DeviceSpec) -> None:
        super().__init__()
        self.spec = spec
        self._index = 0
        self._next: float | None = None

    def start(self, now: float) -> list[Exchange]:
        self._next = now + self.spec.start_after
        return []

    def feed(self, data: bytes, now: float) -> list[Exchange]:
        return []

    def deadline(self) -> float | None:
        return self._next

    def tick(self, now: float) -> list[Exchange]:
        out: list[Exchange] = []
        spec = self.spec
        if self._next is not None and now - self._next > 10 * spec.interval:
            self._next = now  # the process was stopped for a while: do not send a burst to catch up
        while self._next is not None and now >= self._next:
            line = spec.lines[self._index] + spec.eol
            out.append(Exchange(request=b"", emissions=[Emission(0.0, line)]))
            self._index += 1
            self._next += spec.interval
            if self._index == len(spec.lines):
                if spec.loop:
                    self._index = 0
                else:
                    self._next = None
        return out


def _expand(template: bytes, match: re.Match[str] | None, line: str) -> bytes:
    def one(m: re.Match[bytes]) -> bytes:
        key = m.group(1)
        if key == b"line":
            return line.encode("latin-1")
        if match is None:
            return b""
        return (match.group(int(key)) or "").encode("latin-1")

    return _TEMPLATE.sub(one, template)


class ScriptDevice(_Plain):
    def __init__(self, spec: DeviceSpec) -> None:
        super().__init__()
        self.spec = spec
        self.state = spec.initial
        self.prompts = dict(spec.prompts)
        self._used: set[int] = set()
        self._line = bytearray()
        self._after_cr = False

    def _prompt(self) -> bytes:
        return self.prompts.get(self.state, b"")

    def start(self, now: float) -> list[Exchange]:
        data = self.spec.greeting + self._prompt()
        return [Exchange(request=b"", emissions=[Emission(0.0, data)], faultable=False)] if data else []

    def feed(self, data: bytes, now: float) -> list[Exchange]:
        out: list[Exchange] = []
        echo = bytearray()

        def flush_echo() -> None:
            if echo:
                out.append(Exchange(request=b"", emissions=[Emission(0.0, bytes(echo))], faultable=False))
                echo.clear()

        for byte in data:
            if byte in (0x0D, 0x0A):
                if byte == 0x0A and self._after_cr:
                    self._after_cr = False
                    continue
                self._after_cr = byte == 0x0D
                line = bytes(self._line)
                self._line.clear()
                if self.spec.echo:
                    echo += b"\r\n"
                flush_echo()
                out.append(self._answer(line))
                continue
            self._after_cr = False
            if byte in (0x08, 0x7F):
                if self._line:
                    del self._line[-1]
                    if self.spec.echo:
                        echo += b"\b \b"
                continue
            if len(self._line) >= MAX_LINE:
                self._events.append(f"line longer than {MAX_LINE} bytes: the rest is discarded")
                continue
            self._line.append(byte)
            if self.spec.echo:
                echo.append(byte)
        flush_echo()
        return out

    def _answer(self, raw: bytes) -> Exchange:
        line = raw.decode("latin-1")
        for i, rule in enumerate(self.spec.rules):
            if rule.once and i in self._used:
                continue
            if rule.state is not None and rule.state != self.state:
                continue
            match = rule.pattern.search(line)
            if match is None:
                continue
            if rule.once:
                self._used.add(i)
            if rule.lines:
                emissions = [
                    Emission(
                        rule.delay if n == 0 else rule.line_delay, _expand(text, match, line) + self.spec.eol
                    )
                    for n, text in enumerate(rule.lines)
                ]
            elif rule.send:
                emissions = [Emission(rule.delay, _expand(rule.send, match, line))]
            else:
                emissions = []
            if rule.next is not None:
                self.state = rule.next
            return Exchange(request=raw, emissions=self._with_prompt(emissions, rule.delay))
        self._events.append(f"no rule matched {line!r} in state {self.state!r}")
        default = (
            [Emission(0.0, _expand(self.spec.default_send, None, line))] if self.spec.default_send else []
        )
        return Exchange(request=raw, emissions=self._with_prompt(default, 0.0))

    def _with_prompt(self, emissions: list[Emission], delay: float) -> list[Emission]:
        prompt = self._prompt()
        if not prompt:
            return emissions
        if not emissions:
            return [Emission(delay, prompt)]
        last = emissions[-1]
        return [*emissions[:-1], Emission(last.delay, last.data + prompt)]


def make_behaviour(spec: DeviceSpec):
    if spec.kind == "modbus-rtu":
        return ModbusSlave(spec)
    if spec.kind == "script":
        return ScriptDevice(spec)
    if spec.kind == "stream":
        return StreamDevice(spec)
    return EchoDevice()
