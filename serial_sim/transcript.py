"""The record of what crossed the wire: one JSON object per line, easy to read back, grep and diff."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path


class Transcript:
    def __init__(self, path: str | Path | None, clock=time.monotonic) -> None:
        self._clock = clock
        self._start = clock()
        self._lock = threading.Lock()
        self._file = open(path, "w", encoding="utf-8") if path else None  # noqa: SIM115 - closed in close()

    def _write(self, record: dict) -> None:
        if self._file is None:
            return
        record = {"t": round(self._clock() - self._start, 3), **record}
        with self._lock:
            self._file.write(json.dumps(record, sort_keys=False) + "\n")
            self._file.flush()

    def rx(self, device: str, data: bytes) -> None:
        self._write({"device": device, "dir": "rx", "hex": data.hex(" "), "text": printable(data)})

    def tx(self, device: str, data: bytes) -> None:
        self._write({"device": device, "dir": "tx", "hex": data.hex(" "), "text": printable(data)})

    def event(self, device: str, text: str) -> None:
        self._write({"device": device, "event": text})

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None


_ESCAPES = {0x5C: "\\\\", 0x0D: "\\r", 0x0A: "\\n", 0x09: "\\t"}


def printable(data: bytes) -> str:
    """The bytes as text: printable ASCII as it is, the rest as an escape (\\r, \\n, \\x1b)."""
    out = []
    for b in data:
        if b in _ESCAPES:
            out.append(_ESCAPES[b])
        elif 32 <= b < 127:
            out.append(chr(b))
        else:
            out.append(f"\\x{b:02x}")
    return "".join(out)


def read(path: str | Path) -> Iterator[dict]:
    with open(path, encoding="utf-8") as f:
        for number, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number}: not JSON: {exc}") from exc
            if not isinstance(record, dict) or "t" not in record or "device" not in record:
                raise ValueError(f"{path}:{number}: not a serial-sim transcript line")
            yield record


def format_record(record: dict, *, show_time: bool = True, show_text: bool = False) -> str:
    head = f"{record['t']:8.3f}  " if show_time else ""
    device = record["device"]
    if "event" in record:
        return f"{head}{device:<10} --  {record['event']}"
    line = f"{head}{device:<10} {record['dir']}  {record['hex']}"
    if show_text and record.get("text"):
        line += f"   |{record['text']}|"
    return line
