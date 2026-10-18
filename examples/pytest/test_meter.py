"""The software under test would go here; this one just opens the port like any program would."""

import os
import select
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "demo"))

from ask import crc16  # noqa: E402  - the small master from the demo


def test_the_meter_answers_a_read(meter_port):
    fd = os.open(meter_port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        request = bytes.fromhex("11 03 0000 0001")
        os.write(fd, request + crc16(request))
        select.select([fd], [], [], 2)
        reply = os.read(fd, 256)
    finally:
        os.close(fd)
    assert reply[:3] == bytes.fromhex("11 03 02")
    assert int.from_bytes(reply[3:5], "big") == 2301
