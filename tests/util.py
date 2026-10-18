"""Helpers shared by the tests."""

from __future__ import annotations

import os
import select
import shutil
import threading
import time
from contextlib import contextmanager

import pytest

from serial_sim.crc import append_crc
from serial_sim.engine import Engine
from serial_sim.modbus import ModbusSlave
from serial_sim.scenario import DeviceSpec, parse
from serial_sim.transcript import Transcript

MODBUS_HEAD = 'seed = 1\n[[device]]\nname = "m"\nkind = "modbus-rtu"\nunit = 17\n'


def spec_of(text: str) -> DeviceSpec:
    return parse(text).devices[0]


def slave(blocks: str = "", head: str = "") -> ModbusSlave:
    return ModbusSlave(spec_of(MODBUS_HEAD + head + blocks))


def block(table: str, address: int, values: list, extra: str = "") -> str:
    return f'[[device.block]]\ntable = "{table}"\naddress = {address}\nvalues = {list(values)!r}\n{extra}\n'.replace(
        "True", "true"
    ).replace("False", "false")


def req(pdu: bytes | str, unit: int = 17) -> bytes:
    if isinstance(pdu, str):
        pdu = bytes.fromhex(pdu)
    return append_crc(bytes((unit,)) + pdu)


def replies(exchanges) -> list[bytes]:
    return [e.data for ex in exchanges for e in ex.emissions]


def ask(s: ModbusSlave, pdu: bytes | str, unit: int = 17) -> bytes | None:
    """One request, one reply (None when the device stays silent)."""
    out = replies(s.feed(req(pdu, unit), 0.0))
    assert len(out) <= 1
    return out[0] if out else None


def tool_missing(name: str) -> bool:
    return shutil.which(name) is None


def require_or_skip(ok: bool, what: str) -> None:
    """A missing tool skips the test, unless CI says every tool must be there."""
    if ok:
        return
    if os.environ.get("SERIAL_SIM_REQUIRE_TOOLS"):
        pytest.fail(f"{what} is required here (SERIAL_SIM_REQUIRE_TOOLS is set) but is not available")
    pytest.skip(f"{what} is not available")


@contextmanager
def running(text: str, *, log=None, link_dir=None):
    """The engine for a scenario, served from a thread; yields (paths, engine)."""
    engine = Engine(parse(text), link_dir=link_dir, transcript=Transcript(log))
    paths = engine.open()
    stop = threading.Event()
    failure: list[BaseException] = []

    def serve() -> None:
        try:
            engine.serve(stop.is_set)
        except BaseException as exc:
            failure.append(exc)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield paths, engine
    finally:
        stop.set()
        thread.join(5)
        engine.close()
    assert not failure, failure


class Port:
    """A client's view of a link: open it, write, read with a deadline."""

    def __init__(self, path: str) -> None:
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)

    def write(self, data: bytes) -> None:
        os.write(self.fd, data)

    def read(self, want: int = 1, timeout: float = 2.0, *, quiet: float | None = None) -> bytes:
        """Read until `want` bytes arrived or `timeout` passed; with `quiet`, until the line is silent that long."""
        end = time.monotonic() + timeout
        got = b""
        while time.monotonic() < end:
            wait = min(quiet if quiet is not None and got else 0.05, end - time.monotonic())
            r, _, _ = select.select([self.fd], [], [], max(wait, 0))
            if r:
                try:
                    got += os.read(self.fd, 4096)
                except BlockingIOError:
                    continue
                if quiet is None and len(got) >= want:
                    return got
            elif quiet is not None and got:
                return got
        return got

    def drain(self) -> bytes:
        return self.read(0, 0.05, quiet=0.05)

    def close(self) -> None:
        os.close(self.fd)

    def __enter__(self) -> Port:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
