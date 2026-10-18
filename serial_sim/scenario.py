"""Scenario files: TOML in, checked and typed devices out.

Everything a scenario may say is listed in `SECTIONS`. The same list drives the checks (unknown keys are errors, with
a suggestion) and the tables in the README, so the documentation cannot drift from what is accepted.
"""

from __future__ import annotations

import difflib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

MAX_FILE_BYTES = 1 << 20
KINDS = ("modbus-rtu", "script", "stream", "echo")
GENERIC_FAULTS = ("drop", "delay", "corrupt", "garbage", "truncate", "split", "duplicate")
MODBUS_FAULTS = ("corrupt-crc", "exception", "wrong-unit")
TABLES = ("holding", "input", "coil", "discrete")
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
RESERVED_ENV = ("SERIAL_SIM_DEVICES", "SERIAL_SIM_REQUIRE_TOOLS")  # used by `exec` and by the tests


class ScenarioError(Exception):
    """The scenario file cannot be used. The message says where and why."""


@dataclass(frozen=True)
class Key:
    name: str
    type: str
    default: str
    doc: str
    required: bool = False


SECTIONS: dict[str, tuple[Key, ...]] = {
    "top level": (
        Key(
            "seed",
            "int",
            "0",
            "Seed of the random choices (probabilities, flipped bits, noise). Same seed, same run.",
        ),
        Key("device", "array of tables", "", "The devices: one `[[device]]` table each. At least one."),
    ),
    "[[device]], every kind": (
        Key(
            "name",
            "string",
            "",
            "Letters, digits, `_` and `-`, starting with a letter, at most 32. Unique.",
            True,
        ),
        Key("kind", "string", "", "`modbus-rtu`, `script`, `stream` or `echo`.", True),
        Key(
            "link",
            "string",
            "a temporary directory",
            "Path of the symbolic link that points at the device node. Created at start, removed at exit.",
        ),
        Key(
            "baud",
            "int",
            "0",
            "Replies take `length * char_bits / baud` seconds to arrive. 0 means no delay.",
        ),
        Key(
            "char_bits",
            "int",
            "10",
            "Bits per character on the wire: start + 8 data + stop = 10; with parity, 11.",
        ),
        Key(
            "bytewise",
            "bool",
            "false",
            "Needs `baud`. Send a reply one character at a time, each when it would have left the line, "
            "instead of as one piece when the last one has. For software with a time limit between characters.",
        ),
        Key(
            "fault", "array of tables", "", "Fault injection rules, applied to replies. See the fault table."
        ),
    ),
    '[[device]] kind = "modbus-rtu"': (
        Key(
            "unit",
            "int",
            "",
            "Slave address, 1 to 247. Frames for other addresses are ignored; address 0 (broadcast) is obeyed for writes and never answered.",
            True,
        ),
        Key(
            "frame_gap_ms",
            "number",
            "50",
            "Silence that ends a frame. Bytes left over after it are discarded; a frame of an unknown function is only answered after it.",
        ),
        Key(
            "strict_addresses",
            "bool",
            "true",
            "An address outside every block is exception 2 (illegal data address). With `false` every address exists and starts at 0.",
        ),
        Key("block", "array of tables", "", "The data. See the block table."),
    ),
    "[[device.block]]": (
        Key(
            "table",
            "string",
            "",
            "`holding`, `input` (16-bit registers), `coil` or `discrete` (single bits).",
            True,
        ),
        Key("address", "int", "", "First address, 0 to 65535.", True),
        Key(
            "values",
            "array",
            "",
            "One entry per address: 0 to 65535 for registers; 0, 1, `true` or `false` for bits.",
            True,
        ),
        Key(
            "writable",
            "bool",
            "true",
            "Holding registers and coils only. A write to a block with `false` is exception 2.",
        ),
        Key(
            "step",
            "int",
            "0",
            "Registers only. After every successful read of the block, each value goes up by this much (modulo 65536).",
        ),
    ),
    '[[device]] kind = "script"': (
        Key(
            "greeting",
            "string",
            "",
            "Sent once at start. It waits in the device buffer until a client reads it.",
        ),
        Key(
            "initial",
            "string",
            "",
            "The state the dialogue starts in. Its prompt, if any, follows the greeting.",
        ),
        Key(
            "prompts",
            "table of strings",
            "{}",
            "State name to prompt text. After every reply the prompt of the new state is sent.",
        ),
        Key("eol", "string", '"\\r\\n"', "Appended to each line of a `lines` reply."),
        Key("echo", "bool", "false", "Echo every received character back, like a console does."),
        Key("default_send", "string", "", "Reply to a line that no rule matches."),
        Key("rule", "array of tables", "", "What to answer. See the rule table."),
    ),
    "[[device.rule]]": (
        Key(
            "match",
            "string",
            "",
            "Regular expression, searched in the received line (without its terminator). The first rule that fits wins.",
            True,
        ),
        Key("state", "string", "any state", "The rule only applies in this state."),
        Key(
            "send",
            "string",
            "",
            "Reply text. `${line}` is the whole line, `${1}`, `${2}` ... are the groups of `match`.",
        ),
        Key(
            "send_hex",
            "string",
            "",
            "Reply bytes, as hex digits, for binary replies. Not together with `send` or `lines`.",
        ),
        Key(
            "lines",
            "array of strings",
            "",
            "Reply as several lines, each followed by `eol` and `line_delay_ms` apart (a boot log, say).",
        ),
        Key("line_delay_ms", "number", "0", "Pause between the lines of `lines`."),
        Key("delay_ms", "number", "0", "Pause before the reply."),
        Key("next", "string", "", "The state after this rule."),
        Key("once", "bool", "false", "Use the rule once, then ignore it."),
    ),
    '[[device]] kind = "stream"': (
        Key("lines", "array of strings", "", "What the device sends, one line per tick, in order.", True),
        Key("interval_ms", "number", "", "Time between lines.", True),
        Key("loop", "bool", "true", "Start again after the last line. With `false` the stream ends."),
        Key("start_after_ms", "number", "0", "Wait before the first line."),
        Key("eol", "string", '"\\r\\n"', "Appended to each line."),
    ),
    "[[device.fault]]": (
        Key("kind", "string", "", "What goes wrong. See the fault table.", True),
        Key("every", "int", "", "Apply to every n-th request (the 1st request is number 1)."),
        Key(
            "probability",
            "number",
            "",
            "Apply with this probability, 0 to 1, drawn from the seeded generator.",
        ),
        Key("after", "int", "0", "Only requests after this number."),
        Key("until", "int", "", "Only requests up to this number (a burst, or an outage window)."),
        Key(
            "functions", "array of ints", "", "Modbus only: apply only to requests with these function codes."
        ),
        Key("ms", "number", "", "`delay` and `split`: milliseconds. Required for `delay`."),
        Key("count", "int", "3", "`garbage`: number of noise bytes."),
        Key("keep", "int", "half", "`truncate`: number of bytes kept."),
        Key("at", "int", "half", "`split`: the byte where the reply is cut in two."),
        Key("bits", "int", "1", "`corrupt`: number of bits flipped."),
        Key("code", "int", "", "`exception`: the Modbus exception code, 1 to 255. Required for `exception`."),
        Key(
            "unit",
            "int",
            "",
            "`wrong-unit`: the address the reply claims to come from, 0 to 255. Required for `wrong-unit`.",
        ),
    ),
}

FAULT_KINDS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    # kind: (applies to, what the client sees, its own keys)
    "drop": ("all", "No reply at all.", ()),
    "delay": ("all", "The reply comes `ms` late.", ("ms",)),
    "corrupt": ("all", "`bits` bits of the reply are flipped.", ("bits",)),
    "garbage": ("all", "`count` noise bytes come before the reply.", ("count",)),
    "truncate": ("all", "Only the first `keep` bytes arrive.", ("keep",)),
    "split": ("all", "The reply arrives in two parts, `ms` apart, cut at `at`.", ("ms", "at")),
    "duplicate": ("all", "The reply arrives twice.", ()),
    "corrupt-crc": ("modbus-rtu", "The reply is complete but its checksum is wrong.", ()),
    "exception": ("modbus-rtu", "An exception reply with `code` instead of the answer.", ("code",)),
    "wrong-unit": ("modbus-rtu", "The reply carries another address.", ("unit",)),
}
_SELECTORS = ("every", "probability", "after", "until", "functions")


@dataclass(frozen=True)
class FaultSpec:
    kind: str
    every: int | None = None
    probability: float | None = None
    after: int = 0
    until: int | None = None
    functions: tuple[int, ...] = ()
    ms: float = 0.0
    count: int = 3
    keep: int | None = None
    at: int | None = None
    bits: int = 1
    code: int = 0
    unit: int = 0


@dataclass(frozen=True)
class BlockSpec:
    table: str
    address: int
    values: tuple[int, ...]
    writable: bool = True
    step: int = 0


@dataclass(frozen=True)
class RuleSpec:
    pattern: re.Pattern[str]
    state: str | None = None
    send: bytes = b""
    lines: tuple[bytes, ...] = ()
    line_delay: float = 0.0
    delay: float = 0.0
    next: str | None = None
    once: bool = False


@dataclass(frozen=True)
class DeviceSpec:
    name: str
    kind: str
    link: str | None = None
    baud: int = 0
    char_bits: int = 10
    bytewise: bool = False
    faults: tuple[FaultSpec, ...] = ()
    # modbus-rtu
    unit: int = 0
    frame_gap: float = 0.05
    strict_addresses: bool = True
    blocks: tuple[BlockSpec, ...] = ()
    # script
    greeting: bytes = b""
    initial: str = ""
    prompts: tuple[tuple[str, bytes], ...] = ()
    eol: bytes = b"\r\n"
    echo: bool = False
    default_send: bytes = b""
    rules: tuple[RuleSpec, ...] = ()
    # stream
    lines: tuple[bytes, ...] = ()
    interval: float = 0.0
    loop: bool = True
    start_after: float = 0.0

    @property
    def env_name(self) -> str:
        return env_name(self.name)


@dataclass(frozen=True)
class Scenario:
    seed: int
    devices: tuple[DeviceSpec, ...]


def env_name(name: str) -> str:
    return "SERIAL_SIM_" + re.sub(r"[^A-Z0-9]", "_", name.upper())


# ---- checking ----


def _is(value: object, type_: str) -> bool:
    if type_ == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_ == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_ == "string":
        return isinstance(value, str)
    if type_ == "bool":
        return isinstance(value, bool)
    if type_ == "array":
        return isinstance(value, list)
    if type_ == "array of ints":
        return isinstance(value, list) and all(_is(v, "int") for v in value)
    if type_ == "array of strings":
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    if type_ == "table of strings":
        return isinstance(value, dict) and all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()
        )
    if type_ == "array of tables":
        return isinstance(value, list) and all(isinstance(v, dict) for v in value)
    return False


def _table(table: object, keys: tuple[Key, ...], where: str, *, extra: tuple[str, ...] = ()) -> dict:
    """Check one TOML table against its keys: unknown ones, wrong types, missing required ones."""
    if not isinstance(table, dict):
        raise ScenarioError(f"{where}: expected a table")
    known = {k.name: k for k in keys}
    for name in table:
        if name not in known and name not in extra:
            close = difflib.get_close_matches(name, [*known, *extra], n=1)
            hint = f" (did you mean {close[0]!r}?)" if close else ""
            raise ScenarioError(f"{where}: unknown key {name!r}{hint}")
    for key in keys:
        if key.name in table and not _is(table[key.name], key.type):
            raise ScenarioError(f"{where}: {key.name} must be {_article(key.type)}, not {table[key.name]!r}")
        if key.required and key.name not in table:
            raise ScenarioError(f"{where}: {key.name} is required")
    return table


def _article(type_: str) -> str:
    return ("an " if type_[0] in "ai" else "a ") + type_


def _in_range(value: float, low: float, high: float, name: str, where: str) -> None:
    if not low <= value <= high:
        raise ScenarioError(f"{where}: {name} must be between {low:g} and {high:g}, not {value:g}")


def _text(value: str) -> bytes:
    return value.encode("utf-8")


def _fault(raw: object, kind: str, where: str) -> FaultSpec:
    table = _table(raw, SECTIONS["[[device.fault]]"], where)
    fkind = table["kind"]
    if fkind not in FAULT_KINDS:
        close = difflib.get_close_matches(fkind, FAULT_KINDS, n=1)
        hint = f" (did you mean {close[0]!r}?)" if close else ""
        raise ScenarioError(
            f"{where}: unknown fault kind {fkind!r}{hint}; the kinds are {', '.join(FAULT_KINDS)}"
        )
    applies, _, own = FAULT_KINDS[fkind]
    if applies != "all" and applies != kind:
        raise ScenarioError(
            f"{where}: the fault {fkind!r} only exists for {applies} devices, this one is {kind}"
        )
    allowed = {"kind", *_SELECTORS, *own}
    for name in table:
        if name not in allowed:
            raise ScenarioError(f"{where}: {name} does not apply to the fault {fkind!r}")
    if "functions" in table and kind != "modbus-rtu":
        raise ScenarioError(f"{where}: functions only applies to modbus-rtu devices")
    required = {"delay": ("ms",), "exception": ("code",), "wrong-unit": ("unit",)}.get(fkind, ())
    for name in required:
        if name not in table:
            raise ScenarioError(f"{where}: the fault {fkind!r} needs {name}")
    if "every" in table:
        _in_range(table["every"], 1, 10**9, "every", where)
    if "probability" in table:
        p = float(table["probability"])
        if not 0 < p <= 1:
            raise ScenarioError(f"{where}: probability must be above 0 and at most 1, not {p:g}")
    after = table.get("after", 0)
    _in_range(after, 0, 10**9, "after", where)
    if "until" in table:
        _in_range(table["until"], 1, 10**9, "until", where)
        if table["until"] <= after:
            raise ScenarioError(f"{where}: until ({table['until']}) must be larger than after ({after})")
    functions = tuple(table.get("functions", ()))
    for fn in functions:
        _in_range(fn, 1, 127, "functions", where)
    if "ms" in table:
        _in_range(table["ms"], 0, 60_000, "ms", where)
    if "count" in table:
        _in_range(table["count"], 1, 1024, "count", where)
    if "keep" in table:
        _in_range(table["keep"], 0, 65_535, "keep", where)
    if "at" in table:
        _in_range(table["at"], 1, 65_535, "at", where)
    if "bits" in table:
        _in_range(table["bits"], 1, 64, "bits", where)
    if "code" in table:
        _in_range(table["code"], 1, 255, "code", where)
    if "unit" in table:
        _in_range(table["unit"], 0, 255, "unit", where)
    return FaultSpec(
        kind=fkind,
        every=table.get("every"),
        probability=float(table["probability"]) if "probability" in table else None,
        after=after,
        until=table.get("until"),
        functions=functions,
        ms=float(table.get("ms", 20.0 if fkind == "split" else 0.0)),
        count=table.get("count", 3),
        keep=table.get("keep"),
        at=table.get("at"),
        bits=table.get("bits", 1),
        code=table.get("code", 0),
        unit=table.get("unit", 0),
    )


def _block(raw: object, where: str) -> BlockSpec:
    table = _table(raw, SECTIONS["[[device.block]]"], where)
    kind = table["table"]
    if kind not in TABLES:
        raise ScenarioError(f"{where}: table must be one of {', '.join(TABLES)}, not {kind!r}")
    bits = kind in ("coil", "discrete")
    if "writable" in table and kind in ("input", "discrete"):
        raise ScenarioError(f"{where}: {kind} tables are never writable, so writable does not apply")
    if "step" in table and bits:
        raise ScenarioError(f"{where}: step only applies to register tables")
    address = table["address"]
    _in_range(address, 0, 65_535, "address", where)
    raw_values = table["values"]
    if not raw_values:
        raise ScenarioError(f"{where}: values is empty")
    if address + len(raw_values) > 65_536:
        raise ScenarioError(f"{where}: {len(raw_values)} values from address {address} run past 65535")
    values = []
    for i, v in enumerate(raw_values):
        if bits:
            if isinstance(v, bool):
                values.append(int(v))
            elif _is(v, "int") and v in (0, 1):
                values.append(v)
            else:
                raise ScenarioError(f"{where}: values[{i}] must be 0, 1, true or false, not {v!r}")
        else:
            if not _is(v, "int") or not 0 <= v <= 65_535:
                raise ScenarioError(
                    f"{where}: values[{i}] must be a whole number from 0 to 65535 (write a negative number as "
                    f"its 16-bit two's complement), not {v!r}"
                )
            values.append(v)
    step = table.get("step", 0)
    _in_range(step, 0, 65_535, "step", where)
    return BlockSpec(kind, address, tuple(values), table.get("writable", True), step)


def _blocks(raw: list, where: str) -> tuple[BlockSpec, ...]:
    blocks = tuple(_block(b, f"{where}: block {i + 1}") for i, b in enumerate(raw))
    for i, a in enumerate(blocks):
        for j in range(i):
            b = blocks[j]
            if (
                a.table == b.table
                and a.address < b.address + len(b.values)
                and b.address < a.address + len(a.values)
            ):
                raise ScenarioError(f"{where}: block {i + 1} overlaps block {j + 1} ({a.table} table)")
    return blocks


def _hex(text: str, where: str) -> bytes:
    cleaned = re.sub(r"[\s_:,-]", "", text)
    try:
        return bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ScenarioError(f"{where}: send_hex is not hex digits: {exc}") from exc


def _rule(raw: object, where: str) -> RuleSpec:
    table = _table(raw, SECTIONS["[[device.rule]]"], where)
    if len(table["match"]) > 500:
        raise ScenarioError(f"{where}: match is longer than 500 characters")
    try:
        pattern = re.compile(table["match"])
    except re.error as exc:
        raise ScenarioError(f"{where}: match is not a regular expression: {exc}") from exc
    given = [k for k in ("send", "send_hex", "lines") if k in table]
    if len(given) > 1:
        raise ScenarioError(f"{where}: use only one of send, send_hex and lines, not {' and '.join(given)}")
    if "line_delay_ms" in table and "lines" not in table:
        raise ScenarioError(f"{where}: line_delay_ms only applies together with lines")
    send = b""
    if "send" in table:
        send = _text(table["send"])
    elif "send_hex" in table:
        send = _hex(table["send_hex"], where)
    for name in ("delay_ms", "line_delay_ms"):
        if name in table:
            _in_range(table[name], 0, 60_000, name, where)
    for n in re.findall(rb"\$\{(\d+)\}", send + b"".join(_text(s) for s in table.get("lines", ()))):
        if int(n) > pattern.groups:
            raise ScenarioError(
                f"{where}: the reply uses ${{{int(n)}}} but match has only {pattern.groups} group(s)"
            )
    return RuleSpec(
        pattern=pattern,
        state=table.get("state"),
        send=send,
        lines=tuple(_text(s) for s in table.get("lines", ())),
        line_delay=float(table.get("line_delay_ms", 0)) / 1000,
        delay=float(table.get("delay_ms", 0)) / 1000,
        next=table.get("next"),
        once=table.get("once", False),
    )


_COMMON = SECTIONS["[[device]], every kind"]
_PER_KIND = {
    "modbus-rtu": SECTIONS['[[device]] kind = "modbus-rtu"'],
    "script": SECTIONS['[[device]] kind = "script"'],
    "stream": SECTIONS['[[device]] kind = "stream"'],
    "echo": (),
}


def _device(raw: object, index: int) -> DeviceSpec:
    where = f"device {index + 1}"
    if not isinstance(raw, dict):
        raise ScenarioError(f"{where}: expected a table")
    if isinstance(raw.get("name"), str):
        where = f"device {raw['name']!r}"
    kind = raw.get("kind")
    if kind not in KINDS:
        if "kind" not in raw:
            raise ScenarioError(f"{where}: kind is required ({', '.join(KINDS)})")
        close = difflib.get_close_matches(str(kind), KINDS, n=1)
        hint = f" (did you mean {close[0]!r}?)" if close else ""
        raise ScenarioError(f"{where}: unknown kind {kind!r}{hint}; the kinds are {', '.join(KINDS)}")
    keys = _COMMON + _PER_KIND[kind]
    table = _table(raw, keys, where)
    name = table["name"]
    if not NAME.match(name):
        raise ScenarioError(
            f"{where}: name {name!r} must start with a letter and use only letters, digits, _ and -"
        )
    if env_name(name) in RESERVED_ENV:
        raise ScenarioError(
            f"{where}: the name {name!r} would be {env_name(name)}, which has another meaning: rename it"
        )
    link = table.get("link")
    if link is not None and (not link or "\0" in link):
        raise ScenarioError(f"{where}: link must be a path")
    baud = table.get("baud", 0)
    if baud != 0:
        _in_range(baud, 50, 4_000_000, "baud", where)
    char_bits = table.get("char_bits", 10)
    _in_range(char_bits, 7, 13, "char_bits", where)
    bytewise = table.get("bytewise", False)
    if bytewise and baud == 0:
        raise ScenarioError(f"{where}: bytewise needs a baud rate (the characters are spaced by it)")
    faults = tuple(_fault(f, kind, f"{where}: fault {i + 1}") for i, f in enumerate(table.get("fault", ())))
    common = {
        "name": name,
        "kind": kind,
        "link": link,
        "baud": baud,
        "char_bits": char_bits,
        "bytewise": bytewise,
        "faults": faults,
    }
    if kind == "modbus-rtu":
        unit = table["unit"]
        _in_range(unit, 1, 247, "unit", where)
        gap = table.get("frame_gap_ms", 50)
        _in_range(gap, 1, 10_000, "frame_gap_ms", where)
        blocks = _blocks(table.get("block", []), where)
        if table.get("strict_addresses", True) and not blocks:
            raise ScenarioError(
                f"{where}: no block, so every address would be illegal (add a [[device.block]])"
            )
        return DeviceSpec(
            **common,
            unit=unit,
            frame_gap=gap / 1000,
            strict_addresses=table.get("strict_addresses", True),
            blocks=blocks,
        )
    if kind == "script":
        rules = tuple(_rule(r, f"{where}: rule {i + 1}") for i, r in enumerate(table.get("rule", ())))
        prompts = table.get("prompts", {})
        initial = table.get("initial", "")
        declared = {*prompts, *(r.state for r in rules if r.state)}
        if initial and initial not in declared:
            raise ScenarioError(f"{where}: initial state {initial!r} has no prompt and no rule")
        for i, r in enumerate(rules):
            if r.next and r.next not in declared:
                known = ", ".join(sorted(declared)) or "none"
                raise ScenarioError(
                    f"{where}: rule {i + 1}: next state {r.next!r} is unknown (known: {known})"
                )
        if not rules and not table.get("default_send") and not table.get("greeting") and not prompts:
            raise ScenarioError(
                f"{where}: a script device with no rule, greeting, prompt or default_send says nothing"
            )
        return DeviceSpec(
            **common,
            greeting=_text(table.get("greeting", "")),
            initial=initial,
            prompts=tuple((s, _text(t)) for s, t in prompts.items()),
            eol=_text(table.get("eol", "\r\n")),
            echo=table.get("echo", False),
            default_send=_text(table.get("default_send", "")),
            rules=rules,
        )
    if kind == "stream":
        lines = table["lines"]
        if not lines:
            raise ScenarioError(f"{where}: lines is empty")
        _in_range(table["interval_ms"], 1, 3_600_000, "interval_ms", where)
        _in_range(table.get("start_after_ms", 0), 0, 3_600_000, "start_after_ms", where)
        return DeviceSpec(
            **common,
            lines=tuple(_text(s) for s in lines),
            interval=float(table["interval_ms"]) / 1000,
            loop=table.get("loop", True),
            start_after=float(table.get("start_after_ms", 0)) / 1000,
            eol=_text(table.get("eol", "\r\n")),
        )
    return DeviceSpec(**common)


def parse(text: str) -> Scenario:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ScenarioError(f"not valid TOML: {exc}") from exc
    top = _table(data, SECTIONS["top level"], "scenario")
    seed = top.get("seed", 0)
    _in_range(seed, 0, 2**63 - 1, "seed", "scenario")
    raw_devices = top.get("device")
    if not raw_devices:
        raise ScenarioError("scenario: there is no [[device]]")
    devices = tuple(_device(d, i) for i, d in enumerate(raw_devices))
    seen: dict[str, str] = {}
    links: dict[str, str] = {}
    for d in devices:
        if d.env_name in seen:
            raise ScenarioError(
                f"devices {seen[d.env_name]!r} and {d.name!r} would both be {d.env_name}: rename one"
            )
        seen[d.env_name] = d.name
        if d.link:
            if d.link in links:
                raise ScenarioError(f"devices {links[d.link]!r} and {d.name!r} use the same link {d.link!r}")
            links[d.link] = d.name
    return Scenario(seed=seed, devices=devices)


def load(path: str | Path) -> Scenario:
    p = Path(path)
    try:
        size = p.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ScenarioError(f"{p}: {size} bytes is too large for a scenario (limit {MAX_FILE_BYTES})")
        text = p.read_bytes().decode("utf-8")
    except OSError as exc:
        raise ScenarioError(f"{p}: {exc.strerror or exc}") from exc
    except UnicodeDecodeError as exc:
        raise ScenarioError(f"{p}: not UTF-8 text: {exc}") from exc
    try:
        return parse(text)
    except ScenarioError as exc:
        raise ScenarioError(f"{p}: {exc}") from exc
