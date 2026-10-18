"""The transcript file: what is written, what is read back, what is refused."""

import pytest

from serial_sim.transcript import Transcript, format_record, printable, read


def test_bytes_are_shown_so_that_the_text_is_unambiguous():
    assert printable(b"a ~") == "a ~"
    assert printable(b"\r\n\t") == "\\r\\n\\t"
    assert printable(b"\x00\x1b\x7f\xff") == "\\x00\\x1b\\x7f\\xff"
    assert printable(b"\\") == "\\\\"  # a backslash is doubled, or "\\r" could be a carriage return


def test_blank_lines_are_skipped_when_reading(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        '{"t": 0.1, "device": "a", "event": "x"}\n\n   \n{"t": 0.2, "device": "a", "event": "y"}\n'
    )
    assert [r["event"] for r in read(path)] == ["x", "y"]


@pytest.mark.parametrize("text", ['{"t": 1}', '{"device": "a"}', "[1, 2]", '"text"'])
def test_a_line_needs_both_a_time_and_a_device(tmp_path, text):
    path = tmp_path / "t.jsonl"
    path.write_text(text + "\n")
    with pytest.raises(ValueError, match="not a serial-sim transcript line"):
        list(read(path))


def test_times_count_from_the_start_of_the_transcript(tmp_path):
    now = [100.0]
    path = tmp_path / "t.jsonl"
    transcript = Transcript(path, clock=lambda: now[0])
    now[0] = 100.5
    transcript.event("a", "x")
    transcript.close()
    assert next(read(path))["t"] == 0.5


def test_a_record_is_formatted_with_or_without_its_time():
    record = {"t": 1.5, "device": "m", "dir": "rx", "hex": "01 02", "text": "\\x01\\x02"}
    assert format_record(record) == "   1.500  m          rx  01 02"
    assert format_record(record, show_time=False) == "m          rx  01 02"
    assert format_record(record, show_time=False, show_text=True).endswith("01 02   |\\x01\\x02|")
    assert format_record({"t": 2, "device": "m", "event": "hello"}, show_time=False) == "m          --  hello"
