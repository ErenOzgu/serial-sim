"""A tiny Modbus RTU master, standard library only: what the README's transcript is made from.

It has its own CRC on purpose: it does not import anything from serial_sim.
Run it through `serial-sim exec examples/demo/meter.toml -- python3 examples/demo/ask.py`.
"""

import os
import select
import struct
import time


def crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack("<H", crc)


def exchange(fd: int, pdu: bytes, unit: int = 17, wait: float = 0.5) -> bytes:
    frame = bytes((unit,)) + pdu
    os.write(fd, frame + crc16(frame))
    end = time.monotonic() + wait
    got = b""
    while time.monotonic() < end:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if ready:
            got += os.read(fd, 256)
        elif got:
            break
    return got


def main() -> None:
    fd = os.open(os.environ["SERIAL_SIM_METER"], os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    exchange(fd, bytes.fromhex("03 0000 0003"))  # read three holding registers
    exchange(fd, bytes.fromhex("06 0002 0063"))  # set register 2 to 99
    exchange(fd, bytes.fromhex("03 0002 0001"))  # and read it back
    exchange(fd, bytes.fromhex("04 0000 0001"))  # an input register that counts up when read
    exchange(fd, bytes.fromhex("04 0000 0001"))
    exchange(fd, bytes.fromhex("03 0064 0001"))  # an address that does not exist: exception 2
    exchange(fd, bytes.fromhex("03 0000 0001"), unit=18)  # another device's address: no answer


if __name__ == "__main__":
    main()
