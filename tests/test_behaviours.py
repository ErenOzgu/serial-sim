"""The script, stream and echo devices, tested as pure state machines: bytes and a clock in, bytes out."""

import random

import pytest

from serial_sim.behaviours import MAX_LINE, EchoDevice, ScriptDevice, StreamDevice, make_behaviour
from serial_sim.scenario import ScenarioError

from .util import replies, spec_of

CONSOLE = """
[[device]]
name = "console"
kind = "script"
greeting = "\\r\\nBoard v1\\r\\n"
initial = "login"
echo = false

[device.prompts]
login = "login: "
password = "Password: "
shell = "# "

[[device.rule]]
state = "login"
match = "^root$"
next = "password"

[[device.rule]]
state = "password"
match = "^(\\\\w+)$"
send = "\\r\\nWelcome, root (typed ${1})\\r\\n"
next = "shell"

[[device.rule]]
state = "shell"
match = "^uname( -a)?$"
send = "Linux board 6.1\\r\\n"

[[device.rule]]
state = "shell"
match = "^reboot$"
lines = ["Stopping services", "Rebooting"]
line_delay_ms = 100
delay_ms = 10
next = "login"

[[device.rule]]
match = "^whoami$"
send = "root\\r\\n"
once = true
"""


def console(extra: str = "") -> ScriptDevice:
    return make_behaviour(spec_of(CONSOLE + extra))


def talk(dev, text: bytes, now: float = 0.0) -> bytes:
    return b"".join(replies(dev.feed(text, now)))


def test_the_greeting_and_the_first_prompt_are_sent_at_start_and_are_not_faultable():
    dev = console()
    (ex,) = dev.start(0.0)
    assert ex.emissions[0].data == b"\r\nBoard v1\r\nlogin: "
    assert ex.faultable is False


def test_a_dialogue_walks_through_the_states():
    dev = console()
    assert talk(dev, b"root\r") == b"Password: "
    assert dev.state == "password"
    assert talk(dev, b"hunter2\r\n") == b"\r\nWelcome, root (typed hunter2)\r\n# "
    assert talk(dev, b"uname\r") == b"Linux board 6.1\r\n# "
    assert talk(dev, b"uname -a\n") == b"Linux board 6.1\r\n# "


def test_a_line_that_no_rule_matches_gets_the_default_reply_and_the_prompt():
    dev = console()
    talk(dev, b"root\r")
    talk(dev, b"x\r")
    assert talk(dev, b"frobnicate\r") == b"# "  # no default_send: just the prompt again
    assert any("no rule matched" in e for e in dev.drain_events())


def test_default_send_can_quote_the_line():
    spec = spec_of(
        '[[device]]\nname = "c"\nkind = "script"\ninitial = "main"\ndefault_send = "unknown: ${line}\\r\\n"\n'
        '[device.prompts]\nmain = "> "\n'
    )
    dev = make_behaviour(spec)
    assert talk(dev, b"hello\r") == b"unknown: hello\r\n> "


def test_a_rule_with_lines_sends_them_with_the_delay_between_them_and_the_prompt_at_the_end():
    dev = console()
    talk(dev, b"root\r")
    talk(dev, b"x\r")
    (ex,) = dev.feed(b"reboot\r", 0.0)
    assert [(e.delay, e.data) for e in ex.emissions] == [
        (0.01, b"Stopping services\r\n"),
        (0.1, b"Rebooting\r\nlogin: "),
    ]
    assert dev.state == "login"


def test_a_once_rule_is_used_once():
    dev = console()
    talk(dev, b"root\r")
    talk(dev, b"x\r")
    assert talk(dev, b"whoami\r") == b"root\r\n# "
    assert talk(dev, b"whoami\r") == b"# "


def test_cr_lf_and_crlf_each_end_one_line():
    dev = console()
    out = replies(dev.feed(b"root\r\nroot\nroot\r", 0.0))
    assert len(out) == 3  # three lines, whatever the terminator; the state moves, so the answers differ
    dev = console()
    assert len(replies(dev.feed(b"a\r", 0.0))) == 1
    assert len(replies(dev.feed(b"\n", 0.0))) == 0  # the LF that completes a CRLF split across two reads


def test_a_line_may_arrive_one_byte_at_a_time():
    dev = console()
    out = b""
    for b in b"root\r":
        out += talk(dev, bytes((b,)))
    assert out == b"Password: "


def test_backspace_edits_the_line():
    dev = console()
    assert talk(dev, b"roox\x08t\r") == b"Password: "
    assert talk(dev, b"\x7f\x7f\x08pw\r") == b"\r\nWelcome, root (typed pw)\r\n# "


def test_echo_repeats_what_was_typed_and_is_not_faultable():
    dev = ScriptDevice(spec_of(CONSOLE.replace("echo = false", "echo = true")))
    exchanges = dev.feed(b"ro\x08oot\r", 0.0)
    assert exchanges[0].faultable is False
    assert exchanges[0].emissions[0].data == b"ro\b \boot\r\n"
    assert exchanges[1].faultable is True and exchanges[1].emissions[0].data == b"Password: "


def test_echo_is_sent_before_the_answer_of_every_line_when_several_arrive_together():
    dev = ScriptDevice(spec_of(CONSOLE.replace("echo = false", "echo = true")))
    out = replies(dev.feed(b"root\rx\r", 0.0))
    assert out == [b"root\r\n", b"Password: ", b"x\r\n", b"\r\nWelcome, root (typed x)\r\n# "]


def test_a_huge_line_is_cut_and_the_device_goes_on():
    dev = console()
    out = talk(dev, b"a" * (MAX_LINE + 500) + b"\r")
    assert out == b"login: "  # no rule matches: the prompt comes back
    assert any("longer than" in e for e in dev.drain_events())
    assert talk(dev, b"root\r") == b"Password: "


@pytest.mark.parametrize("seed", range(20))
def test_random_input_never_breaks_a_script_device(seed):
    rng = random.Random(seed)
    dev = console()
    for _ in range(100):
        chunk = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 40)))
        for ex in dev.feed(chunk, 0.0):
            assert all(isinstance(e.data, bytes) for e in ex.emissions)


def test_a_device_with_a_greeting_only_says_it_once():
    dev = make_behaviour(spec_of('[[device]]\nname = "g"\nkind = "script"\ngreeting = "hello\\r\\n"\n'))
    assert [e.data for ex in dev.start(0.0) for e in ex.emissions] == [b"hello\r\n"]
    assert dev.feed(b"anything\r", 0.0)[0].emissions == []


# ---- the scenario checks of script devices ----


@pytest.mark.parametrize(
    ("snippet", "message"),
    [
        ('[[device.rule]]\nmatch = "("', "not a regular expression"),
        ('[[device.rule]]\nmatch = "a"\nsend = "x"\nsend_hex = "00"', "only one of send, send_hex and lines"),
        ('[[device.rule]]\nmatch = "a"\nsend_hex = "zz"', "not hex digits"),
        ('[[device.rule]]\nmatch = "a"\nsend = "${2}"', "uses ${2} but match has only 0 group"),
        ('[[device.rule]]\nmatch = "a"\nlines = ["${1}"]', "uses ${1} but match has only 0 group"),
        ('[[device.rule]]\nmatch = "a"\nline_delay_ms = 5', "only applies together with lines"),
        ('[[device.rule]]\nmatch = "a"\nnext = "nowhere"', "next state 'nowhere' is unknown"),
        (
            'initial = "nowhere"\n[[device.rule]]\nmatch = "a"',
            "initial state 'nowhere' has no prompt and no rule",
        ),
        ('[[device.rule]]\nsend = "x"', "match is required"),
        ('[[device.rule]]\nmatch = "a"\nsned = "x"', "did you mean 'send'"),
    ],
)
def test_bad_script_scenarios_are_refused(snippet, message):
    with pytest.raises(ScenarioError, match=message.replace("$", r"\$").replace("{", r"\{")):
        spec_of('[[device]]\nname = "s"\nkind = "script"\n' + snippet)


def test_a_script_device_that_could_never_say_anything_is_refused():
    with pytest.raises(ScenarioError, match="says nothing"):
        spec_of('[[device]]\nname = "s"\nkind = "script"\n')


def test_send_hex_may_be_written_with_separators():
    dev = make_behaviour(
        spec_of(
            '[[device]]\nname = "s"\nkind = "script"\n[[device.rule]]\nmatch = "x"\nsend_hex = "de ad:be-ef"\n'
        )
    )
    assert talk(dev, b"x\r") == bytes.fromhex("deadbeef")


# ---- stream ----

STREAM = '[[device]]\nname = "gps"\nkind = "stream"\nlines = ["$A", "$B", "$C"]\ninterval_ms = 100\n'


def test_a_stream_sends_one_line_per_interval_in_order_and_loops():
    dev = make_behaviour(spec_of(STREAM))
    dev.start(10.0)
    assert dev.deadline() == pytest.approx(10.0)
    got = []
    for step in range(8):
        got += [(round(step * 0.1, 1), b) for b in replies(dev.tick(10.0 + step * 0.1))]
    assert [b for _, b in got] == [b"$A\r\n", b"$B\r\n", b"$C\r\n"] * 2 + [b"$A\r\n", b"$B\r\n"]


def test_a_stream_may_start_later_and_end():
    dev = make_behaviour(
        spec_of(STREAM.replace("interval_ms = 100", "interval_ms = 100\nstart_after_ms = 500\nloop = false"))
    )
    dev.start(0.0)
    assert dev.tick(0.4) == []
    assert replies(dev.tick(0.5)) == [b"$A\r\n"]
    assert replies(dev.tick(0.7)) == [b"$B\r\n", b"$C\r\n"]
    assert dev.deadline() is None
    assert dev.tick(99.0) == []


def test_a_stream_that_was_stalled_does_not_send_a_burst():
    dev = make_behaviour(spec_of(STREAM))
    dev.start(0.0)
    dev.tick(0.0)
    out = replies(dev.tick(600.0))
    assert len(out) == 1


def test_a_stream_ignores_what_it_receives_and_uses_a_custom_eol():
    dev = make_behaviour(spec_of(STREAM.replace("interval_ms = 100", 'interval_ms = 100\neol = "|"')))
    assert dev.feed(b"hello", 0.0) == []
    dev.start(0.0)
    assert replies(dev.tick(0.0)) == [b"$A|"]


@pytest.mark.parametrize(
    ("snippet", "message"),
    [
        ("lines = []\ninterval_ms = 10", "lines is empty"),
        ('lines = ["a"]', "interval_ms is required"),
        ('lines = ["a"]\ninterval_ms = 0', "interval_ms must be between 1"),
        ("interval_ms = 10", "lines is required"),
    ],
)
def test_bad_stream_scenarios_are_refused(snippet, message):
    with pytest.raises(ScenarioError, match=message):
        spec_of('[[device]]\nname = "s"\nkind = "stream"\n' + snippet)


# ---- echo ----


def test_echo_sends_back_what_it_gets_and_counts_each_read_as_a_request():
    dev = make_behaviour(spec_of('[[device]]\nname = "e"\nkind = "echo"\n'))
    assert isinstance(dev, EchoDevice)
    (ex,) = dev.feed(b"abc", 0.0)
    assert ex.emissions[0].data == b"abc" and ex.faultable
    assert dev.start(0.0) == [] and dev.tick(1.0) == [] and dev.deadline() is None


def test_the_kind_decides_the_behaviour():
    assert isinstance(make_behaviour(spec_of(STREAM)), StreamDevice)


def test_a_rule_matches_anywhere_in_the_line():
    dev = make_behaviour(
        spec_of('[[device]]\nname = "s"\nkind = "script"\n[[device.rule]]\nmatch = "up"\nsend = "ok"\n')
    )
    assert replies(dev.feed(b"set-up-now\r", 0.0)) == [b"ok"]


def test_a_rule_without_text_still_waits_before_the_new_prompt():
    text = """
[[device]]
name = "s"
kind = "script"
initial = "a"

[device.prompts]
a = "a> "
b = "b> "

[[device.rule]]
state = "a"
match = "^go$"
delay_ms = 30
next = "b"
"""
    dev = make_behaviour(spec_of(text))
    (ex,) = dev.feed(b"go\r", 0.0)
    assert [(e.delay, e.data) for e in ex.emissions] == [(0.03, b"b> ")]
