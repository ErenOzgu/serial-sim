"""The Modbus slave, tested with the worked examples of the Modbus Application Protocol specification, and with
frames cut and mangled the way a noisy line does it. No terminal is involved here."""

import random

import pytest

from serial_sim.crc import append_crc, crc_ok
from serial_sim.modbus import MAX_BUFFER, frame_length

from .util import ask, block, replies, req, slave

# ---- the examples of the specification ----


def test_read_coils_example():
    # Coils 20-56 (addresses 19-55), 37 of them: CD 6B B2 0E 1B
    bits = []
    for byte in bytes.fromhex("cd6bb20e1b"):
        bits += [(byte >> i) & 1 for i in range(8)]
    s = slave(block("coil", 19, bits[:37]))
    assert ask(s, "01 0013 0025") == req("01 05 cd6bb20e1b")


def test_read_discrete_inputs_example():
    bits = []
    for byte in bytes.fromhex("acdb35"):
        bits += [(byte >> i) & 1 for i in range(8)]
    s = slave(block("discrete", 196, bits[:22]))
    assert ask(s, "02 00c4 0016") == req("02 03 acdb35")


def test_read_holding_registers_example():
    s = slave(block("holding", 107, [555, 0, 100]))
    assert ask(s, "03 006b 0003") == req("03 06 022b 0000 0064")


def test_read_input_registers_example():
    s = slave(block("input", 8, [10]))
    assert ask(s, "04 0008 0001") == req("04 02 000a")


def test_write_single_coil_example():
    s = slave(block("coil", 172, [0]))
    assert ask(s, "05 00ac ff00") == req("05 00ac ff00")
    assert s.data["coil"][172] == 1
    assert ask(s, "05 00ac 0000") == req("05 00ac 0000")
    assert s.data["coil"][172] == 0


def test_write_single_register_example():
    s = slave(block("holding", 1, [0]))
    assert ask(s, "06 0001 0003") == req("06 0001 0003")
    assert s.data["holding"][1] == 3


def test_write_multiple_coils_example():
    s = slave(block("coil", 19, [0] * 10))
    assert ask(s, "0f 0013 000a 02 cd01") == req("0f 0013 000a")
    # CD 01 = 1100 1101, 0000 0001: the first ten coils, least significant bit first
    assert [s.data["coil"][19 + i] for i in range(10)] == [1, 0, 1, 1, 0, 0, 1, 1, 1, 0]


def test_write_multiple_registers_example():
    s = slave(block("holding", 1, [0, 0]))
    assert ask(s, "10 0001 0002 04 000a 0102") == req("10 0001 0002")
    assert [s.data["holding"][1], s.data["holding"][2]] == [10, 258]


# ---- exceptions ----


def exc(fn: int, code: int) -> bytes:
    return req(bytes((fn | 0x80, code)))


@pytest.mark.parametrize(
    "pdu", ["03 0000 0000", "03 0000 007e", "01 0000 0000", "01 0000 07d1", "02 0000 07d1"]
)
def test_a_read_of_no_or_too_many_items_is_illegal_data_value(pdu):
    s = slave(block("holding", 0, [1] * 200) + block("coil", 0, [1] * 8) + block("discrete", 0, [1] * 8))
    assert ask(s, pdu) == exc(int(pdu[:2], 16), 3)


def test_the_largest_legal_reads_work():
    s = slave(block("holding", 0, [7] * 125) + block("coil", 0, [1] * 2000))
    assert len(ask(s, "03 0000 007d")) == 1 + 1 + 1 + 250 + 2
    assert len(ask(s, "01 0000 07d0")) == 1 + 1 + 1 + 250 + 2


def test_an_address_outside_every_block_is_illegal_data_address():
    s = slave(block("holding", 10, [1, 2, 3]))
    assert ask(s, "03 0009 0001") == exc(3, 2)
    assert ask(s, "03 000b 0003") == exc(3, 2)  # starts inside, runs out
    assert ask(s, "03 000a 0003") is not None and ask(s, "03 000a 0003")[1] == 3
    assert ask(s, "03 fffe 0003") == exc(3, 2)  # past the end of the address space
    assert ask(s, "04 000a 0001") == exc(4, 2)  # another table


def test_a_block_may_start_at_zero_and_the_gap_between_two_blocks_is_illegal():
    s = slave(block("holding", 0, [1, 2]) + block("holding", 4, [5]))
    assert ask(s, "03 0000 0002")[1] == 3
    assert ask(s, "03 0001 0004") == exc(3, 2)


def test_non_strict_addresses_exist_and_hold_zero():
    s = slave(block("holding", 0, [9]), head="strict_addresses = false\n")
    assert ask(s, "03 0000 0003") == req("03 06 0009 0000 0000")
    assert ask(s, "06 1234 0abc") == req("06 1234 0abc")
    assert ask(s, "03 1234 0001") == req("03 02 0abc")
    assert ask(s, "03 ffff 0001") == req("03 02 0000")
    assert ask(s, "03 ffff 0002") == exc(3, 2)


@pytest.mark.parametrize(
    "pdu",
    [
        "05 0000 1234",
        "05 0000 00ff",
        "0f 0000 0000 00",
        "0f 0000 0008 02 ffff",
        "0f 0000 0009 01 ff",
        "10 0000 0000 00",
    ],
)
def test_malformed_writes_are_illegal_data_value(pdu):
    s = slave(block("coil", 0, [0] * 16) + block("holding", 0, [0] * 4))
    assert ask(s, pdu) == exc(int(pdu[:2], 16), 3)


def test_a_write_with_a_wrong_byte_count_is_illegal_data_value():
    s = slave(block("holding", 0, [0] * 4))
    assert ask(s, "10 0000 0002 02 0001") == exc(16, 3)
    assert ask(s, "10 0000 0002 06 000100020003") == exc(16, 3)
    assert s.data["holding"] == {0: 0, 1: 0, 2: 0, 3: 0}


def test_a_write_to_a_read_only_block_or_a_missing_address_is_illegal_data_address():
    s = slave(
        block("holding", 0, [1, 2], "writable = false")
        + block("holding", 2, [3, 4])
        + block("coil", 0, [0], "writable = false")
    )
    assert ask(s, "06 0000 0009") == exc(6, 2)
    assert ask(s, "10 0001 0002 04 00090009") == exc(16, 2)  # touches register 1, which is read-only
    assert ask(s, "10 0002 0002 04 00090008") == req("10 0002 0002")
    assert ask(s, "06 0009 0001") == exc(6, 2)
    assert ask(s, "05 0000 ff00") == exc(5, 2)
    assert s.data["holding"] == {0: 1, 1: 2, 2: 9, 3: 8}
    assert ask(s, "03 0000 0002") == req("03 04 0001 0002")  # read-only can still be read


def test_a_failed_multiple_write_changes_nothing():
    s = slave(block("holding", 0, [1, 1, 1], "writable = true"))
    assert ask(s, "10 0001 0003 06 000900090009") == exc(16, 2)  # the third address does not exist
    assert s.data["holding"] == {0: 1, 1: 1, 2: 1}


@pytest.mark.parametrize("fn", [7, 8, 11, 12, 17, 22, 23, 0x2B, 0x41])
def test_other_functions_are_illegal_function(fn):
    s = slave(block("holding", 0, [1]))
    body = bytes((fn,)) + bytes(range(1, 5))
    if fn == 23:
        body = bytes((23,)) + bytes(8) + bytes((2,)) + bytes(2)
    if fn in (7, 11, 12, 17):
        body = bytes((fn,))
    if fn == 22:
        body = bytes((22,)) + bytes(6)
    out = replies(s.feed(req(body), 0.0))
    if frame_length(req(body)) == 0:  # a function whose length is unknown is answered after the frame gap
        assert out == []
        out = replies(s.tick(1.0))
    assert out == [exc(fn, 1)]


# ---- data that changes ----


def test_step_counts_up_on_every_read_and_wraps():
    s = slave(block("input", 0, [65534, 5], "step = 1"))
    assert ask(s, "04 0000 0002") == req("04 04 fffe 0005")
    assert ask(s, "04 0000 0002") == req("04 04 ffff 0006")
    assert ask(s, "04 0000 0002") == req("04 04 0000 0007")


def test_step_only_moves_what_was_read():
    s = slave(block("holding", 0, [10, 20], "step = 5"))
    ask(s, "03 0001 0001")
    assert s.data["holding"] == {0: 10, 1: 25}


def test_a_refused_read_does_not_step():
    s = slave(block("holding", 0, [10], "step = 5"))
    assert ask(s, "03 0000 0002") == exc(3, 2)
    assert s.data["holding"][0] == 10


# ---- who is spoken to ----


def test_a_request_for_another_address_is_ignored():
    s = slave(block("holding", 0, [1]))
    assert replies(s.feed(req("03 0000 0001", unit=18), 0.0)) == []
    assert "ignored a frame for unit 18" in s.drain_events()[0]


def test_a_broadcast_write_is_obeyed_and_not_answered():
    s = slave(block("holding", 0, [1]))
    out = s.feed(req("06 0000 0042", unit=0), 0.0)
    assert replies(out) == []
    assert s.data["holding"][0] == 0x42
    assert any("broadcast" in e for e in s.drain_events())


def test_a_broadcast_read_is_ignored():
    s = slave(block("holding", 0, [1]))
    assert s.feed(req("03 0000 0001", unit=0), 0.0) == []


def test_the_reply_carries_the_devices_own_address_and_a_good_checksum():
    s = slave(block("holding", 0, [1]))
    reply = ask(s, "03 0000 0001")
    assert reply[0] == 17 and crc_ok(reply)


# ---- finding frames in a stream of bytes ----


def test_a_frame_arriving_in_pieces_is_answered_once_complete():
    s = slave(block("holding", 0, [1, 2]))
    frame = req("03 0000 0002")
    out = []
    for i, b in enumerate(frame):
        out += replies(s.feed(bytes((b,)), i * 0.001))
    assert out == [req("03 04 0001 0002")]


def test_two_frames_in_one_read_are_both_answered():
    s = slave(block("holding", 0, [1, 2]))
    out = replies(s.feed(req("03 0000 0001") + req("03 0001 0001"), 0.0))
    assert out == [req("03 02 0001"), req("03 02 0002")]


def test_noise_before_a_frame_is_skipped():
    s = slave(block("holding", 0, [1]))
    out = replies(s.feed(b"\x00\xff\x55" + req("03 0000 0001"), 0.0))
    out += replies(s.tick(1.0))  # a noise byte that looks like an unknown function waits for the frame gap
    assert out == [req("03 02 0001")]
    assert any("bad checksum" in e for e in s.drain_events())


def test_a_frame_with_a_bad_checksum_is_not_answered():
    s = slave(block("holding", 0, [1]))
    bad = req("03 0000 0001")[:-1] + b"\x00"
    assert replies(s.feed(bad, 0.0)) == []
    assert replies(s.tick(1.0)) == []
    assert s.deadline() is None


def test_a_cut_off_frame_is_discarded_after_the_gap_and_the_next_one_is_fine():
    s = slave(block("holding", 0, [1]))
    assert replies(s.feed(req("03 0000 0001")[:5], 0.0)) == []
    assert s.deadline() == pytest.approx(0.05)
    assert replies(s.tick(0.04)) == []  # not yet
    assert replies(s.tick(0.06)) == []
    assert s.deadline() is None
    assert replies(s.feed(req("03 0000 0001"), 1.0)) == [req("03 02 0001")]
    assert any("incomplete frame" in e for e in s.drain_events())


def test_the_frame_gap_is_a_setting():
    s = slave(block("holding", 0, [1]), head="frame_gap_ms = 200\n")
    s.feed(b"\x11\x03", 0.0)
    assert s.deadline() == pytest.approx(0.2)


def test_a_valid_frame_of_an_unknown_function_is_answered_after_the_gap_only():
    s = slave(block("holding", 0, [1]))
    frame = req("64 01 02 03")
    assert replies(s.feed(frame, 0.0)) == []
    assert replies(s.tick(0.01)) == []
    assert replies(s.tick(0.06)) == [exc(0x64, 1)]


def test_a_known_function_stuck_behind_an_unknown_one_is_found_after_the_gap():
    s = slave(block("holding", 0, [1]))
    out = replies(s.feed(b"\x11\x7e" + req("03 0000 0001"), 0.0))
    out += replies(s.tick(1.0))
    assert out == [req("03 02 0001")]


def test_noise_is_bounded():
    s = slave(block("holding", 0, [1]))
    s.feed(bytes(random.Random(1).randrange(256) for _ in range(5000)), 0.0)
    assert len(s._buf) <= MAX_BUFFER
    assert any("noise" in e for e in s.drain_events())


@pytest.mark.parametrize("seed", range(30))
def test_random_bytes_never_break_the_parser_and_a_clean_frame_still_works(seed):
    rng = random.Random(seed)
    s = slave(block("holding", 0, [1, 2, 3]) + block("coil", 0, [1, 0, 1]))
    now = 0.0
    for _ in range(40):
        chunk = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 30)))
        for ex in s.feed(chunk, now):
            for part in ex.emissions:
                assert crc_ok(part.data)  # whatever it answers is a well-formed frame
        now += rng.choice([0.0, 0.001, 0.2])
        s.tick(now)
    now += 1.0
    s.tick(now)
    assert s.deadline() is None
    assert replies(s.feed(req("03 0000 0003"), now)) == [req("03 06 000100020003")]


def test_frame_length():
    assert frame_length(bytes((17, 3))) == 8
    assert frame_length(bytes((17, 16, 0, 0, 0, 2))) is None  # the byte count is not here yet
    assert frame_length(bytes((17, 16, 0, 0, 0, 2, 4))) == 13
    assert frame_length(bytes((17, 15, 0, 0, 0, 9, 2))) == 11
    assert frame_length(bytes((17, 0x64))) == 0
    assert frame_length(b"\x11") is None


def test_append_crc_is_what_the_slave_checks():
    assert crc_ok(append_crc(b"\x11\x03\x00\x00\x00\x01"))


def test_a_write_that_reaches_into_a_read_only_block_is_refused_whole():
    s = slave(
        block("holding", 0, [1, 2])
        + block("holding", 2, [3], "writable = false")
        + block("coil", 0, [0, 0, 0])
        + block("coil", 3, [0], "writable = false")
    )
    assert ask(s, "10 0001 0002 04 00090009") == exc(16, 2)  # register 1 is writable, register 2 is not
    assert ask(s, "0f 0002 0002 01 03") == exc(15, 2)  # coil 2 is writable, coil 3 is not
    assert s.data["holding"] == {0: 1, 1: 2, 2: 3}
    assert s.data["coil"] == {0: 0, 1: 0, 2: 0, 3: 0}


def test_the_largest_legal_writes_work_and_one_more_is_illegal_data_value():
    s = slave(head="strict_addresses = false\n")
    coils = f"0f 0000 {1968:04x} {246:02x} " + "ff" * 246
    assert ask(s, coils) == req(f"0f 0000 {1968:04x}")
    assert s.data["coil"][1967] == 1
    assert ask(s, f"0f 0000 {1969:04x} {247:02x} " + "ff" * 247) == exc(15, 3)
    registers = f"10 0000 {123:04x} {246:02x} " + "0001" * 123
    assert ask(s, registers) == req(f"10 0000 {123:04x}")
    assert s.data["holding"][122] == 1
    assert ask(s, f"10 0000 {124:04x} {248:02x} " + "0001" * 124) == exc(16, 3)


@pytest.mark.parametrize(
    ("pdu", "table", "address", "value"),
    [
        ("05 0001 ff00", "coil", 1, 1),
        ("06 0000 0042", "holding", 0, 0x42),
        ("0f 0000 0002 01 03", "coil", 1, 1),
        ("10 0000 0001 02 0042", "holding", 0, 0x42),
    ],
)
def test_a_broadcast_is_obeyed_for_every_kind_of_write_and_never_answered(pdu, table, address, value):
    s = slave(block("holding", 0, [1]) + block("coil", 0, [0, 0]))
    assert replies(s.feed(req(pdu, unit=0), 0.0)) == []
    assert s.data[table][address] == value


def test_two_or_three_bytes_that_never_become_a_frame_are_dropped_together():
    s = slave(block("holding", 0, [1]))
    assert s.feed(b"\x11\x03\x00", 0.0) == []
    assert s.tick(s.gap + 0.01) == []
    assert s.drain_events() == ["dropped 3 bytes: incomplete frame"]
