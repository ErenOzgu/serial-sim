"""The simulator on real pseudo-terminals, with the standard library as the client. Linux behaviour of ptys."""

import json
import os
import time
from pathlib import Path

import pytest

from serial_sim.engine import Engine
from serial_sim.exchange import Emission, Exchange
from serial_sim.link import LinkError
from serial_sim.scenario import parse
from serial_sim.transcript import Transcript, read

from .util import MODBUS_HEAD, Port, block, req, running

ECHO = '[[device]]\nname = "e"\nkind = "echo"\n'


def test_echo_comes_back_through_a_real_port():
    with running(ECHO) as (paths, _), Port(paths["e"]) as port:
        port.write(b"hello")
        assert port.read(5) == b"hello"


def test_the_link_is_a_symbolic_link_to_a_terminal_in_raw_mode():
    import termios

    with running(ECHO) as (paths, engine):
        link = Path(paths["e"])
        assert link.is_symlink()
        node = os.readlink(link)
        assert node.startswith("/dev/pts/") or node.startswith("/dev/tty")
        with Port(paths["e"]) as port:
            attrs = termios.tcgetattr(port.fd)
            assert not attrs[3] & (
                termios.ICANON | termios.ECHO
            )  # lflag: no line editing, no echo of its own
            port.write(b"\x03\x04\x1a\r\n")  # control characters pass through untouched
            assert port.read(5) == b"\x03\x04\x1a\r\n"


def test_a_greeting_written_before_anyone_opens_the_port_waits_for_the_first_reader():
    text = '[[device]]\nname = "g"\nkind = "script"\ngreeting = "hello\\r\\n"\n'
    with running(text) as (paths, _):
        time.sleep(0.2)  # the greeting has been sent by now, and nobody has the port open
        with Port(paths["g"]) as port:
            assert port.read(7) == b"hello\r\n"
        with Port(paths["g"]) as port:
            assert port.read(1, 0.3) == b""  # and it is not repeated for the next client


def test_a_client_can_close_and_open_the_port_again_and_again():
    with running(ECHO) as (paths, _):
        for i in range(5):
            with Port(paths["e"]) as port:
                port.write(b"x%d" % i)
                assert port.read(2) == b"x%d" % i


def test_two_devices_are_independent():
    text = (
        ECHO
        + '[[device]]\nname = "f"\nkind = "script"\n[[device.rule]]\nmatch = "^ping$"\nsend = "pong\\r\\n"\n'
    )
    with running(text) as (paths, _), Port(paths["e"]) as a, Port(paths["f"]) as b:
        a.write(b"abc")
        b.write(b"ping\r")
        assert a.read(3) == b"abc"
        assert b.read(6) == b"pong\r\n"


def test_replies_take_as_long_as_the_baud_rate_says():
    text = ECHO.replace('kind = "echo"', 'kind = "echo"\nbaud = 1200')
    with running(text) as (paths, _), Port(paths["e"]) as port:
        start = time.monotonic()
        port.write(b"x" * 12)  # 12 characters x 10 bits at 1200 baud = 100 ms
        assert port.read(12) == b"x" * 12
        took = time.monotonic() - start
    assert 0.09 <= took < 0.5


def test_replies_are_not_slower_without_a_baud_rate():
    with running(ECHO) as (paths, _), Port(paths["e"]) as port:
        start = time.monotonic()
        port.write(b"x" * 12)
        assert port.read(12) == b"x" * 12
        assert time.monotonic() - start < 0.09


def test_a_stream_ticks_at_its_interval():
    text = '[[device]]\nname = "s"\nkind = "stream"\nlines = ["a", "b"]\ninterval_ms = 60\n'
    with running(text) as (paths, _), Port(paths["s"]) as port:
        chunks, marks = [], []
        while len(chunks) < 4:
            chunk = port.read(3, 1.0)
            assert chunk, "the stream stopped"
            chunks.append(chunk)
            marks.append(time.monotonic())
    assert chunks == [b"a\r\n", b"b\r\n", b"a\r\n", b"b\r\n"]
    gaps = [b - a for a, b in zip(marks, marks[1:], strict=False)]
    assert min(gaps) >= 0.03
    assert 0.04 <= sum(gaps) / len(gaps) <= 0.2


def test_a_dropped_reply_is_silence_for_that_request_only():
    text = ECHO + '[[device.fault]]\nkind = "drop"\nevery = 2\n'
    with running(text) as (paths, _), Port(paths["e"]) as port:
        got = []
        for word in (b"one", b"two", b"three", b"four"):
            port.write(word)
            got.append(port.read(len(word), 0.3))
        assert got == [b"one", b"", b"three", b""]


def test_a_split_reply_arrives_in_two_parts_with_the_pause_between():
    text = ECHO + '[[device.fault]]\nkind = "split"\nms = 200\nat = 3\n'
    with running(text) as (paths, _), Port(paths["e"]) as port:
        port.write(b"abcdef")
        first = port.read(3, 1.0)
        seen = time.monotonic()
        rest = port.read(3, 1.0)
        assert (first, rest) == (b"abc", b"def")
        assert time.monotonic() - seen >= 0.15


def test_a_duplicated_reply_arrives_twice():
    text = ECHO + '[[device.fault]]\nkind = "duplicate"\n'
    with running(text) as (paths, _), Port(paths["e"]) as port:
        port.write(b"ab")
        assert port.read(4) == b"abab"
        assert port.read(1, 0.2) == b""


def test_a_delayed_reply_is_late():
    text = ECHO + '[[device.fault]]\nkind = "delay"\nms = 250\n'
    with running(text) as (paths, _), Port(paths["e"]) as port:
        start = time.monotonic()
        port.write(b"x")
        assert port.read(1) == b"x"
        assert time.monotonic() - start >= 0.22


def test_modbus_over_a_real_port_answers_like_the_pure_slave():
    text = MODBUS_HEAD + block("holding", 0, [1, 2, 3])
    with running(text) as (paths, _), Port(paths["m"]) as port:
        port.write(req("03 0000 0003"))
        assert port.read(11) == req("03 06 000100020003")
        port.write(req("03 0009 0001"))
        assert port.read(5) == req("83 02")


def test_a_cut_off_modbus_request_is_forgotten_after_the_gap():
    text = MODBUS_HEAD + "frame_gap_ms = 60\n" + block("holding", 0, [1])
    with running(text) as (paths, _), Port(paths["m"]) as port:
        port.write(req("03 0000 0001")[:5])
        time.sleep(0.2)
        port.write(req("03 0000 0001"))
        assert port.read(7) == req("03 02 0001")


def test_a_request_split_in_two_writes_is_answered():
    text = MODBUS_HEAD + block("holding", 0, [1])
    with running(text) as (paths, _), Port(paths["m"]) as port:
        frame = req("03 0000 0001")
        port.write(frame[:3])
        time.sleep(0.01)
        port.write(frame[3:])
        assert port.read(7) == req("03 02 0001")


def faulty_run(seed: int) -> list[bytes]:
    text = (
        f"seed = {seed}\n"
        + MODBUS_HEAD.split("\n", 1)[1]
        + block("holding", 0, [1, 2, 3])
        + '[[device.fault]]\nkind = "garbage"\ncount = 4\nprobability = 0.5\n'
        + '[[device.fault]]\nkind = "corrupt"\nbits = 2\nprobability = 0.5\n'
    )
    out = []
    with running(text) as (paths, _), Port(paths["m"]) as port:
        for _ in range(20):
            port.write(req("03 0000 0003"))
            out.append(port.read(1, 0.5, quiet=0.05))
    return out


def test_the_same_seed_breaks_the_same_way_and_another_seed_does_not():
    a = faulty_run(11)
    assert a == faulty_run(11)
    assert a != faulty_run(12)
    assert len({*a}) > 3  # it really is a mix of intact and broken replies


def test_the_transcript_records_both_directions_events_and_faults(tmp_path):
    log = tmp_path / "t.jsonl"
    text = MODBUS_HEAD + block("holding", 0, [1]) + '[[device.fault]]\nkind = "corrupt-crc"\nevery = 2\n'
    with running(text, log=log) as (paths, _), Port(paths["m"]) as port:
        port.write(req("03 0000 0001"))
        port.read(7)
        port.write(req("03 0000 0001"))
        port.read(7)
        port.write(b"\x00\x01\x02\x03\x04\x05\x06\x07")  # noise
        time.sleep(0.2)
    records = list(read(log))
    sent = [(r["dir"], r["hex"]) for r in records if "dir" in r]
    assert sent[0] == ("rx", req("03 0000 0001").hex(" "))
    assert sent[1] == ("tx", req("03 02 0001").hex(" "))
    assert sent[3][1] != req("03 02 0001").hex(" ") and sent[3][1][:-3] == req("03 02 0001").hex(" ")[:-3]
    events = [r["event"] for r in records if "event" in r]
    assert "fault corrupt-crc on request 2" in events
    assert any("bad checksum" in e or "unknown function" in e for e in events)
    assert all(r["device"] == "m" for r in records)
    assert [r["t"] for r in records] == sorted(r["t"] for r in records)


def test_a_client_that_never_reads_cannot_make_the_simulator_grow_without_bound(tmp_path):
    log = tmp_path / "t.jsonl"
    text = '[[device]]\nname = "s"\nkind = "stream"\nlines = ["' + "x" * 200 + '"]\ninterval_ms = 1\n'
    with running(text, log=log) as (paths, engine):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            time.sleep(0.2)
            device = engine.devices[0]
            if len(device._out) > 60_000:
                break
        else:
            pytest.fail("the stream never backed up")
        time.sleep(0.3)
        assert len(engine.devices[0]._out) <= 65_536
    events = [r.get("event", "") for r in read(log)]
    assert any("is not reading" in e for e in events)


# ---- links ----


def test_the_links_disappear_with_the_engine(tmp_path):
    engine = Engine(parse(ECHO), link_dir=tmp_path)
    paths = engine.open()
    assert Path(paths["e"]).is_symlink()
    engine.close()
    assert not os.path.lexists(paths["e"])
    assert list(tmp_path.iterdir()) == []


def test_without_a_directory_the_engine_makes_one_and_removes_it():
    engine = Engine(parse(ECHO))
    paths = engine.open()
    directory = Path(paths["e"]).parent
    assert directory.is_dir() and directory.name.startswith("serial-sim-")
    engine.close()
    assert not directory.exists()


def test_a_scenario_link_and_a_command_line_link_win_over_the_directory(tmp_path):
    text = ECHO.replace('kind = "echo"', f'kind = "echo"\nlink = "{tmp_path}/fixed"')
    with Engine(parse(text), link_dir=tmp_path / "ignored") as engine:
        assert engine.paths["e"] == f"{tmp_path}/fixed"
    with Engine(parse(text), links={"e": str(tmp_path / "cli")}) as engine:
        assert engine.paths["e"] == str(tmp_path / "cli")


def test_a_stale_link_is_replaced_and_a_real_file_is_not(tmp_path):
    stale = tmp_path / "e"
    stale.symlink_to("/dev/null")
    with Engine(parse(ECHO), link_dir=tmp_path) as engine:
        assert os.readlink(stale) == os.readlink(engine.paths["e"])
    assert not os.path.lexists(stale)
    stale.write_text("precious")
    with pytest.raises(LinkError, match="is not a symbolic link"):
        Engine(parse(ECHO), link_dir=tmp_path).open()
    assert stale.read_text() == "precious"


def test_a_failure_halfway_leaves_nothing_behind(tmp_path):
    text = ECHO + '[[device]]\nname = "f"\nkind = "echo"\n'
    (tmp_path / "f").write_text("in the way")
    engine = Engine(parse(text), link_dir=tmp_path)
    with pytest.raises(LinkError):
        engine.open()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f"]


def test_a_link_in_a_missing_directory_is_a_link_error(tmp_path):
    with pytest.raises(LinkError, match="cannot create the link"):
        Engine(parse(ECHO), link_dir=tmp_path / "nope").open()


def test_a_link_override_for_an_unknown_device_is_refused():
    with pytest.raises(LinkError, match="names no device"):
        Engine(parse(ECHO), links={"zzz": "/tmp/x"})


def test_closing_twice_is_harmless(tmp_path):
    engine = Engine(parse(ECHO), link_dir=tmp_path)
    engine.open()
    engine.close()
    engine.close()


def test_serving_stops_promptly_when_asked():
    import threading

    engine = Engine(parse(ECHO))
    engine.open()
    stop = threading.Event()
    thread = threading.Thread(target=engine.serve, args=(stop.is_set,))
    thread.start()
    time.sleep(0.1)
    start = time.monotonic()
    stop.set()
    thread.join(2)
    engine.close()
    assert not thread.is_alive() and time.monotonic() - start < 0.3


def test_the_transcript_file_is_valid_json_lines(tmp_path):
    log = tmp_path / "t.jsonl"
    with running(ECHO, log=log) as (paths, _), Port(paths["e"]) as port:
        port.write(b"hi\r\x1b")
        port.read(4)
    lines = Path(log).read_text().splitlines()
    assert lines
    records = [json.loads(line) for line in lines]
    assert records[0]["text"] == "hi\\r\\x1b" and records[0]["hex"] == "68 69 0d 1b"


def test_a_transcript_without_a_file_costs_nothing():
    t = Transcript(None)
    t.rx("a", b"x")
    t.tx("a", b"y")
    t.event("a", "z")
    t.close()


# ---- found by mutating the engine: what the tests above did not pin down ----


def test_a_script_devices_greeting_and_echo_are_not_answers_that_a_fault_can_drop():
    text = (
        '[[device]]\nname = "c"\nkind = "script"\necho = true\ngreeting = "hello\\r\\n"\n'
        '[[device.rule]]\nmatch = ".*"\nsend = "ok\\r\\n"\n[[device.fault]]\nkind = "drop"\n'
    )
    with running(text) as (paths, _), Port(paths["c"]) as port:
        assert port.read(7) == b"hello\r\n"
        port.write(b"hi\r")
        assert port.read(4) == b"hi\r\n"  # the echo arrives, the answer was dropped
        assert port.read(1, 0.3) == b""


def test_a_fault_on_a_request_that_gets_no_reply_is_not_reported(tmp_path):
    log = tmp_path / "t.jsonl"
    text = MODBUS_HEAD + block("holding", 0, [1]) + '[[device.fault]]\nkind = "drop"\n'
    with running(text, log=log) as (paths, _), Port(paths["m"]) as port:
        port.write(req("06 0000 0042", unit=0))  # a broadcast: obeyed, never answered
        time.sleep(0.3)
        port.write(req("03 0000 0001"))
        time.sleep(0.3)
    events = [r["event"] for r in read(log) if "event" in r]
    assert "fault drop on request 1" not in events
    assert "fault drop on request 2" in events


def test_every_device_draws_from_its_own_generator(tmp_path):
    both = ECHO + '[[device]]\nname = "f"\nkind = "echo"\n'
    with Engine(parse(both), link_dir=tmp_path) as engine:
        a, b = engine.devices
        first = [a.rng.random() for _ in range(3)]
        assert first != [b.rng.random() for _ in range(3)]
    with Engine(parse(both), link_dir=tmp_path) as engine:
        assert [engine.devices[0].rng.random() for _ in range(3)] == first
    with Engine(parse("seed = 9\n" + both), link_dir=tmp_path) as engine:
        assert [engine.devices[0].rng.random() for _ in range(3)] != first


def test_replies_to_requests_that_arrive_together_are_sent_one_after_the_other(tmp_path):
    slow = ECHO.replace('kind = "echo"', 'kind = "echo"\nbaud = 100')  # ten bits a character: 0.1 s each
    with Engine(parse(slow), link_dir=tmp_path) as engine:
        device = engine.devices[0]
        device._process([Exchange(request=b"", emissions=[Emission(0.0, b"abcde")])], 0.0)
        device._process([Exchange(request=b"", emissions=[Emission(0.0, b"fg")])], 0.1)
        assert [round(t, 3) for t, _, _ in sorted(device._queue)] == [0.5, 0.7]


def test_a_client_that_does_not_read_loses_the_newest_bytes_not_the_oldest(tmp_path):
    log = tmp_path / "t.jsonl"
    with (
        Engine(parse(ECHO), link_dir=tmp_path, transcript=Transcript(log)) as engine,
        Port(engine.paths["e"]) as port,
    ):
        device = engine.devices[0]
        device._out += b"A" * 100 + b"B" * 69_900
        device.pump(engine.clock())
        assert len(device._out) <= 65_536
        assert port.read(100)[:100] == b"A" * 100
    assert any("4464 bytes lost" in r.get("event", "") for r in read(log))


def test_closing_an_engine_does_not_remove_a_link_that_another_one_made_since(tmp_path):
    first = Engine(parse(ECHO), link_dir=tmp_path)
    first.open()
    second = Engine(parse(ECHO), link_dir=tmp_path)
    second.open()  # replaces the link of the first
    first.close()
    assert os.readlink(tmp_path / "e") == second.devices[0].link.node
    second.close()
    assert not os.path.lexists(tmp_path / "e")
