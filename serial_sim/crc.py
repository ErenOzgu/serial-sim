"""The Modbus RTU checksum: CRC-16, polynomial 0xA001 (reflected), start value 0xFFFF, sent low byte first."""

from __future__ import annotations


def _table() -> tuple[int, ...]:
    out = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        out.append(crc)
    return tuple(out)


_TABLE = _table()


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc = (crc >> 8) ^ _TABLE[(crc ^ byte) & 0xFF]
    return crc


def append_crc(frame: bytes) -> bytes:
    crc = crc16(frame)
    return frame + bytes((crc & 0xFF, crc >> 8))


def crc_ok(frame: bytes) -> bool:
    return len(frame) >= 3 and crc16(frame[:-2]) == frame[-2] | frame[-1] << 8
