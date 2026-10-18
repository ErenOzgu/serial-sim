"""A Modbus RTU slave: frames in, frames out, exceptions where the specification has them.

Supported function codes: 1, 2, 3, 4 (reads), 5, 6, 15, 16 (writes). Codes 7, 8, 11, 12, 17, 22 and 23 are
recognised so that framing stays right and are answered with exception 1, like every unknown code.
"""

from __future__ import annotations

import struct

from .crc import append_crc, crc_ok
from .exchange import Emission, Exchange
from .scenario import DeviceSpec, FaultSpec

ILLEGAL_FUNCTION = 1
ILLEGAL_ADDRESS = 2
ILLEGAL_VALUE = 3

FUNCTIONS = {
    1: "Read Coils",
    2: "Read Discrete Inputs",
    3: "Read Holding Registers",
    4: "Read Input Registers",
    5: "Write Single Coil",
    6: "Write Single Register",
    15: "Write Multiple Coils",
    16: "Write Multiple Registers",
}
EXCEPTIONS = {
    ILLEGAL_FUNCTION: ("Illegal function", "Any other function code (recognised or not)."),
    ILLEGAL_ADDRESS: (
        "Illegal data address",
        "An address outside every block, or a write to a read-only block.",
    ),
    ILLEGAL_VALUE: (
        "Illegal data value",
        "A quantity out of range, a wrong byte count, a coil value other than 0x0000 or 0xFF00.",
    ),
}
READ_FUNCTIONS = {1: "coil", 2: "discrete", 3: "holding", 4: "input"}
WRITE_FUNCTIONS = (5, 6, 15, 16)
MAX_BUFFER = 512  # a frame is at most 256 bytes; keep room for noise in front of one

_FIXED_LENGTH = {1: 8, 2: 8, 3: 8, 4: 8, 5: 8, 6: 8, 7: 4, 8: 8, 11: 4, 12: 4, 17: 4, 22: 10}


def frame_length(buf: bytes | bytearray) -> int | None:
    """Length of the request that starts `buf`; None while more bytes are needed to tell; 0 for a function
    code whose length is not known."""
    if len(buf) < 2:
        return None
    fn = buf[1]
    if fn in _FIXED_LENGTH:
        return _FIXED_LENGTH[fn]
    if fn in (15, 16):
        return 9 + buf[6] if len(buf) >= 7 else None
    if fn == 23:
        return 13 + buf[10] if len(buf) >= 11 else None
    return 0


class ModbusSlave:
    def __init__(self, spec: DeviceSpec) -> None:
        self.unit = spec.unit
        self.strict = spec.strict_addresses
        self.gap = spec.frame_gap
        self.data: dict[str, dict[int, int]] = {t: {} for t in ("holding", "input", "coil", "discrete")}
        self.readonly: dict[str, set[int]] = {t: set() for t in self.data}
        self.step: dict[str, dict[int, int]] = {t: {} for t in self.data}
        for block in spec.blocks:
            for i, value in enumerate(block.values):
                addr = block.address + i
                self.data[block.table][addr] = value
                if not block.writable:
                    self.readonly[block.table].add(addr)
                if block.step:
                    self.step[block.table][addr] = block.step
        self._buf = bytearray()
        self._last_rx = 0.0
        self._events: list[str] = []

    # ---- the Behaviour interface ----

    def start(self, now: float) -> list[Exchange]:
        return []

    def feed(self, data: bytes, now: float) -> list[Exchange]:
        self._buf += data
        if len(self._buf) > MAX_BUFFER:
            self._events.append(f"discarded {len(self._buf) - MAX_BUFFER} bytes of noise")
            del self._buf[: len(self._buf) - MAX_BUFFER]
        self._last_rx = now
        return self._scan(final=False)

    def deadline(self) -> float | None:
        return self._last_rx + self.gap if self._buf else None

    def tick(self, now: float) -> list[Exchange]:
        if self._buf and now >= self._last_rx + self.gap:
            return self._scan(final=True)
        return []

    def drain_events(self) -> list[str]:
        out, self._events = self._events, []
        return out

    # ---- finding frames in the byte stream ----

    def _scan(self, final: bool) -> list[Exchange]:
        buf = self._buf
        out: list[Exchange] = []
        while buf:
            if len(buf) < 4:
                if final:
                    self._drop(len(buf), "incomplete frame")
                    buf.clear()
                break
            n = frame_length(buf)
            if n is None or (n and len(buf) < n):
                if final:
                    self._drop(1, "incomplete frame")
                    del buf[:1]
                    continue
                break
            if n == 0:
                if not final:
                    break
                whole = bytes(buf)
                if crc_ok(whole):
                    buf.clear()
                    out.extend(self._handle(whole))
                    break
                self._drop(1, "unknown function, bad checksum")
                del buf[:1]
                continue
            frame = bytes(buf[:n])
            if crc_ok(frame):
                del buf[:n]
                out.extend(self._handle(frame))
            else:
                self._drop(1, "bad checksum")
                del buf[:1]
        return out

    def _drop(self, count: int, why: str) -> None:
        self._events.append(f"dropped {count} byte{'s' if count != 1 else ''}: {why}")

    # ---- answering ----

    def _handle(self, frame: bytes) -> list[Exchange]:
        unit, fn = frame[0], frame[1]
        if unit not in (self.unit, 0):
            self._events.append(f"ignored a frame for unit {unit}")
            return []
        broadcast = unit == 0
        if broadcast and fn not in WRITE_FUNCTIONS:
            self._events.append(f"ignored broadcast function {fn}")
            return []
        reply = self._answer(frame)
        emissions = [] if broadcast else [Emission(0.0, reply)]
        if broadcast:
            self._events.append(f"broadcast function {fn}, no reply")
        return [Exchange(request=frame, emissions=emissions, function=fn)]

    def _exception(self, fn: int, code: int) -> bytes:
        return append_crc(bytes((self.unit, fn | 0x80, code)))

    def _answer(self, frame: bytes) -> bytes:
        fn = frame[1]
        if fn in READ_FUNCTIONS:
            return self._read(frame, READ_FUNCTIONS[fn])
        if fn == 5:
            return self._write_single_coil(frame)
        if fn == 6:
            return self._write_single_register(frame)
        if fn == 15:
            return self._write_coils(frame)
        if fn == 16:
            return self._write_registers(frame)
        return self._exception(fn, ILLEGAL_FUNCTION)

    def _have(self, table: str, start: int, count: int) -> bool:
        if start + count > 0x10000:
            return False
        if not self.strict:
            return True
        data = self.data[table]
        return all(a in data for a in range(start, start + count))

    def _writable(self, table: str, start: int, count: int) -> bool:
        return self._have(table, start, count) and not any(
            a in self.readonly[table] for a in range(start, start + count)
        )

    def _read(self, frame: bytes, table: str) -> bytes:
        fn = frame[1]
        start, count = struct.unpack(">HH", frame[2:6])
        bits = table in ("coil", "discrete")
        if not 1 <= count <= (2000 if bits else 125):
            return self._exception(fn, ILLEGAL_VALUE)
        if not self._have(table, start, count):
            return self._exception(fn, ILLEGAL_ADDRESS)
        data = self.data[table]
        values = [data.get(a, 0) for a in range(start, start + count)]
        for a in range(start, start + count):
            step = self.step[table].get(a)
            if step:
                data[a] = (data[a] + step) & 0xFFFF
        if bits:
            packed = bytearray((count + 7) // 8)
            for i, v in enumerate(values):
                if v:
                    packed[i // 8] |= 1 << (i % 8)
            body = bytes(packed)
        else:
            body = b"".join(struct.pack(">H", v) for v in values)
        return append_crc(bytes((self.unit, fn, len(body))) + body)

    def _write_single_coil(self, frame: bytes) -> bytes:
        address, value = struct.unpack(">HH", frame[2:6])
        if value not in (0x0000, 0xFF00):
            return self._exception(5, ILLEGAL_VALUE)
        if not self._writable("coil", address, 1):
            return self._exception(5, ILLEGAL_ADDRESS)
        self.data["coil"][address] = 1 if value else 0
        return append_crc(bytes((self.unit,)) + frame[1:6])

    def _write_single_register(self, frame: bytes) -> bytes:
        address, value = struct.unpack(">HH", frame[2:6])
        if not self._writable("holding", address, 1):
            return self._exception(6, ILLEGAL_ADDRESS)
        self.data["holding"][address] = value
        return append_crc(bytes((self.unit,)) + frame[1:6])

    def _write_coils(self, frame: bytes) -> bytes:
        address, count, size = struct.unpack(">HHB", frame[2:7])
        if not 1 <= count <= 1968 or size != (count + 7) // 8:
            return self._exception(15, ILLEGAL_VALUE)
        if not self._writable("coil", address, count):
            return self._exception(15, ILLEGAL_ADDRESS)
        payload = frame[7 : 7 + size]
        for i in range(count):
            self.data["coil"][address + i] = (payload[i // 8] >> (i % 8)) & 1
        return append_crc(bytes((self.unit, 15)) + frame[2:6])

    def _write_registers(self, frame: bytes) -> bytes:
        address, count, size = struct.unpack(">HHB", frame[2:7])
        if not 1 <= count <= 123 or size != 2 * count:
            return self._exception(16, ILLEGAL_VALUE)
        if not self._writable("holding", address, count):
            return self._exception(16, ILLEGAL_ADDRESS)
        for i in range(count):
            (self.data["holding"][address + i],) = struct.unpack(">H", frame[7 + 2 * i : 9 + 2 * i])
        return append_crc(bytes((self.unit, 16)) + frame[2:6])

    # ---- the faults that need to know what a Modbus frame is ----

    def protocol_fault(self, spec: FaultSpec, exchange: Exchange) -> Exchange:
        first = exchange.emissions[0]
        reply = first.data
        if spec.kind == "corrupt-crc":
            reply = reply[:-1] + bytes((reply[-1] ^ 0xFF,))
        elif spec.kind == "exception":
            reply = self._exception(exchange.request[1], spec.code)
        elif spec.kind == "wrong-unit":
            reply = append_crc(bytes((spec.unit,)) + reply[1:-2])
        exchange.emissions = [Emission(first.delay, reply), *exchange.emissions[1:]]
        return exchange
