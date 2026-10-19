"""The loop that connects pseudo-terminals to device behaviours: read, answer, delay, write."""

from __future__ import annotations

import heapq
import random
import selectors
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from . import faults
from .behaviours import make_behaviour
from .exchange import Exchange
from .link import LinkError, PtyLink
from .scenario import DeviceSpec, Scenario
from .transcript import Transcript

MAX_PENDING_OUTPUT = 65_536  # bytes the client has not read yet; a real UART would overrun long before this


class Device:
    def __init__(
        self, spec: DeviceSpec, index: int, seed: int, link: PtyLink, transcript: Transcript
    ) -> None:
        self.spec = spec
        self.link = link
        self.transcript = transcript
        self.behaviour = make_behaviour(spec)
        self.rng = random.Random(f"{seed}:{index}:{spec.name}")
        self.jitter_rng = random.Random(
            f"{seed}:{index}:{spec.name}:jitter"
        )  # apart, so faults draw as before
        self.requests = 0
        self._queue: list[tuple[float, int, bytes]] = []
        self._seq = 0
        self._tx_free = 0.0
        self._out = bytearray()

    # ---- the loop's view ----

    def start(self, now: float) -> None:
        self._process(self.behaviour.start(now), now)

    def on_readable(self, now: float) -> None:
        data = self.link.read()
        if data:
            self.transcript.rx(self.spec.name, data)
            self._process(self.behaviour.feed(data, now), now)
            self._events()

    def pump(self, now: float) -> None:
        self._process(self.behaviour.tick(now), now)
        self._events()
        lost = 0
        while self._queue and self._queue[0][0] <= now:
            _, _, data = heapq.heappop(self._queue)
            self.transcript.tx(self.spec.name, data)
            room = MAX_PENDING_OUTPUT - len(self._out)  # the bound holds at every moment
            self._out += data[:room]
            lost += max(0, len(data) - room)
        if lost:
            self.transcript.event(self.spec.name, f"the client is not reading: {lost} bytes lost")
        if self._out:
            written = self.link.write(bytes(self._out))
            del self._out[:written]

    def deadline(self) -> float | None:
        times = [
            t
            for t in (self.behaviour.deadline(), self._queue[0][0] if self._queue else None)
            if t is not None
        ]
        return min(times) if times else None

    @property
    def backlog(self) -> bool:
        return bool(self._out)

    # ---- what happens to an exchange ----

    def _events(self) -> None:
        for text in self.behaviour.drain_events():
            self.transcript.event(self.spec.name, text)

    def _process(self, exchanges: list[Exchange], now: float) -> None:
        for ex in exchanges:
            if ex.faultable:
                self.requests += 1
                hit = faults.selected(self.spec.faults, self.requests, ex.function, self.rng)
                if hit and ex.emissions:
                    names = " + ".join(s.kind for s in hit)
                    self.transcript.event(self.spec.name, f"fault {names} on request {self.requests}")
                    ex = faults.apply(ex, hit, self.behaviour, self.rng)
            self._schedule(ex, now, self.jitter_rng.uniform(0, self.spec.jitter) if ex.faultable else 0.0)

    def _schedule(self, ex: Exchange, now: float, jitter: float = 0.0) -> None:
        spec = self.spec
        t = max(now, self._tx_free) + (jitter if ex.emissions else 0.0)
        for part in ex.emissions:
            if not part.data:
                continue
            t += part.delay
            if spec.baud and spec.bytewise:
                per_char = spec.char_bits / spec.baud
                for i in range(len(part.data)):
                    t += per_char
                    self._seq += 1
                    heapq.heappush(self._queue, (t, self._seq, part.data[i : i + 1]))
                continue
            if spec.baud:
                t += len(part.data) * spec.char_bits / spec.baud
            self._seq += 1
            heapq.heappush(self._queue, (t, self._seq, part.data))
        self._tx_free = max(self._tx_free, t)


class Engine:
    def __init__(
        self,
        scenario: Scenario,
        *,
        link_dir: str | Path | None = None,
        links: dict[str, str] | None = None,
        transcript: Transcript | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.scenario = scenario
        self.clock = clock
        self.transcript = transcript or Transcript(None, clock)
        self._link_dir = Path(link_dir) if link_dir else None
        self._own_dir: Path | None = None
        self._overrides = links or {}
        self.devices: list[Device] = []
        self.paths: dict[str, str] = {}
        self._sel = selectors.DefaultSelector()
        unknown = set(self._overrides) - {d.name for d in scenario.devices}
        if unknown:
            raise LinkError(f"--link names no device in the scenario: {', '.join(sorted(unknown))}")

    def open(self) -> dict[str, str]:
        """Create every device and its link. Returns the path of each link by device name."""
        try:
            for index, spec in enumerate(self.scenario.devices):
                path = self._path_for(spec)
                link = PtyLink(path)
                device = Device(spec, index, self.scenario.seed, link, self.transcript)
                self.devices.append(device)
                self._sel.register(link.master, selectors.EVENT_READ, device)
                self.paths[spec.name] = str(path)
        except BaseException:
            self.close()
            raise
        return dict(self.paths)

    def _path_for(self, spec: DeviceSpec) -> Path:
        if spec.name in self._overrides:
            return Path(self._overrides[spec.name])
        if spec.link:
            return Path(spec.link)
        if self._link_dir is not None:
            return self._link_dir / spec.name
        if self._own_dir is None:
            self._own_dir = Path(tempfile.mkdtemp(prefix="serial-sim-"))
        return self._own_dir / spec.name

    def serve(self, should_stop: Callable[[], bool], max_poll: float = 0.02) -> None:
        now = self.clock()
        for device in self.devices:
            device.start(now)
        while not should_stop():
            now = self.clock()
            timeout = 0.005 if any(d.backlog for d in self.devices) else max_poll
            for device in self.devices:
                due = device.deadline()
                if due is not None:
                    timeout = min(timeout, max(0.0, due - now))
            for key, _ in self._sel.select(timeout):
                key.data.on_readable(self.clock())
            now = self.clock()
            for device in self.devices:
                device.pump(now)

    def close(self) -> None:
        for device in self.devices:
            try:
                self._sel.unregister(device.link.master)
            except (KeyError, ValueError, OSError):
                pass
            device.link.close()
        self.devices = []
        self._sel.close()
        if self._own_dir is not None:
            shutil.rmtree(self._own_dir, ignore_errors=True)
            self._own_dir = None
        self.transcript.close()

    def __enter__(self) -> Engine:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
