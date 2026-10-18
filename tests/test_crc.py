import random

import pytest

from serial_sim.crc import append_crc, crc16, crc_ok


@pytest.mark.parametrize(
    ("frame", "crc_on_the_wire"),
    [
        ("01030000000a", "c5cd"),  # the example used all over the Modbus documentation
        ("1103006b0003", "7687"),
        ("010400000001", "31ca"),
        ("0207", "4112"),  # the example of the Modbus over serial line specification
    ],
)
def test_known_checksums(frame, crc_on_the_wire):
    assert append_crc(bytes.fromhex(frame)).hex() == frame + crc_on_the_wire


def test_the_checksum_of_a_frame_with_its_checksum_is_zero():
    assert crc16(bytes.fromhex("01030000000ac5cd")) == 0


def test_any_single_flipped_bit_is_detected():
    frame = append_crc(bytes(range(1, 40)))
    assert crc_ok(frame)
    for bit in range(len(frame) * 8):
        broken = bytearray(frame)
        broken[bit // 8] ^= 1 << (bit % 8)
        assert not crc_ok(bytes(broken)), bit


def test_a_frame_that_is_too_short_is_not_ok():
    assert not crc_ok(b"")
    assert not crc_ok(b"\x01\x02")


def test_it_agrees_with_pymodbus_on_random_data():
    framer = pytest.importorskip("pymodbus.framer.rtu").FramerRTU
    rng = random.Random(7)
    for _ in range(200):
        data = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 60)))
        # pymodbus returns the checksum as the number whose big-endian bytes are the ones on the wire
        assert append_crc(data)[-2:] == framer.compute_CRC(data).to_bytes(2, "big")
