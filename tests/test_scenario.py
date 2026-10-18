import random
from pathlib import Path

import pytest

from serial_sim.scenario import SECTIONS, Scenario, ScenarioError, env_name, load, parse

from .util import MODBUS_HEAD, block

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_every_example_scenario_is_valid():
    found = sorted(EXAMPLES.rglob("*.toml"))
    assert found
    for path in found:
        assert isinstance(load(path), Scenario), path


def test_a_complete_scenario_comes_out_typed():
    sc = parse(
        'seed = 9\n[[device]]\nname = "a"\nkind = "modbus-rtu"\nunit = 5\nbaud = 19200\nchar_bits = 11\n'
        "frame_gap_ms = 20\n" + block("holding", 0, [1, 2]) + '[[device.fault]]\nkind = "drop"\nevery = 2\n'
    )
    d = sc.devices[0]
    assert (sc.seed, d.name, d.unit, d.baud, d.char_bits, d.frame_gap) == (9, "a", 5, 19200, 11, 0.02)
    assert d.blocks[0].values == (1, 2) and d.faults[0].every == 2


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "there is no \\[\\[device\\]\\]"),
        ("seed = 1", "there is no \\[\\[device\\]\\]"),
        ("device = []", "there is no \\[\\[device\\]\\]"),
        ("[[device]]\nname=", "not valid TOML"),
        ('seed = "x"\n[[device]]\nname = "a"\nkind = "echo"', "seed must be an int"),
        ('seed = -1\n[[device]]\nname = "a"\nkind = "echo"', "seed must be between"),
        ('sed = 1\n[[device]]\nname = "a"\nkind = "echo"', "unknown key 'sed' \\(did you mean 'seed'"),
        ('[[device]]\nname = "a"', "kind is required"),
        ('[[device]]\nname = "a"\nkind = "modbus"', "unknown kind 'modbus' \\(did you mean 'modbus-rtu'"),
        ('[[device]]\nname = "a"\nkind = 5', "unknown kind 5"),
        ('[[device]]\nkind = "echo"', "name is required"),
        ('[[device]]\nname = "1a"\nkind = "echo"', "must start with a letter"),
        ('[[device]]\nname = "a b"\nkind = "echo"', "must start with a letter"),
        ('[[device]]\nname = "' + "a" * 33 + '"\nkind = "echo"', "must start with a letter"),
        (
            '[[device]]\nname = "a"\nkind = "echo"\n[[device]]\nname = "a"\nkind = "echo"',
            "both be SERIAL_SIM_A",
        ),
        (
            '[[device]]\nname = "a-b"\nkind = "echo"\n[[device]]\nname = "a_b"\nkind = "echo"',
            "both be SERIAL_SIM_A_B",
        ),
        (
            '[[device]]\nname = "a"\nkind = "echo"\nlink = "/x"\n[[device]]\nname = "b"\nkind = "echo"\nlink = "/x"',
            "same link",
        ),
        ('[[device]]\nname = "a"\nkind = "echo"\nlink = ""', "link must be a path"),
        ('[[device]]\nname = "a"\nkind = "echo"\nbaud = 10', "baud must be between 50"),
        ('[[device]]\nname = "a"\nkind = "echo"\nbaud = "fast"', "baud must be an int"),
        ('[[device]]\nname = "a"\nkind = "echo"\nchar_bits = 3', "char_bits must be between 7 and 13"),
        ('[[device]]\nname = "a"\nkind = "echo"\nunit = 3', "unknown key 'unit'"),
        ('[[device]]\nname = "a"\nkind = "echo"\nbaud = true', "baud must be an int"),
        ('[[device]]\nname = "a"\nkind = "echo"\nlink = 3', "link must be a string"),
        ("device = [1]", "device must be an array of tables"),
    ],
)
def test_bad_scenarios_say_where_and_why(text, message):
    with pytest.raises(ScenarioError, match=message):
        parse(text)


@pytest.mark.parametrize(
    ("extra", "blocks", "message"),
    [
        ("", "", "no block, so every address would be illegal"),
        ("frame_gap_ms = 0\n", block("holding", 0, [1]), "frame_gap_ms must be between 1"),
        ("", block("rom", 0, [1]), "table must be one of holding, input, coil, discrete"),
        ("", block("holding", 70000, [1]), "address must be between 0 and 65535"),
        ("", block("holding", 65535, [1, 2]), "run past 65535"),
        ("", block("holding", 0, []), "values is empty"),
        ("", block("holding", 0, [65536]), "values\\[0\\] must be a whole number from 0 to 65535"),
        ("", block("holding", 0, [-1]), "two's complement"),
        ("", block("holding", 0, [1.5]), "values\\[0\\] must be a whole number"),
        ("", block("coil", 0, [2]), "must be 0, 1, true or false"),
        ("", block("coil", 0, [1], "step = 1"), "step only applies to register tables"),
        ("", block("input", 0, [1], "writable = true"), "input tables are never writable"),
        ("", block("discrete", 0, [1], "writable = false"), "discrete tables are never writable"),
        ("", block("holding", 0, [1], "step = 70000"), "step must be between 0 and 65535"),
        ("", block("holding", 0, [1, 2]) + block("holding", 1, [3]), "block 2 overlaps block 1"),
        ("", block("holding", 0, [1]) + block("input", 0, [3]), None),  # same address, different table: fine
        ("", block("holding", 0, [1], "colour = 3"), "unknown key 'colour'"),
        ("", block("holding", 0, [1], "adress = 3"), "unknown key 'adress' \\(did you mean 'address'"),
    ],
)
def test_bad_modbus_scenarios(extra, blocks, message):
    text = MODBUS_HEAD + extra + blocks
    if message is None:
        assert parse(text)
        return
    with pytest.raises(ScenarioError, match=message):
        parse(text)


def test_bits_may_be_written_as_booleans_or_ones_and_zeros():
    sc = parse(MODBUS_HEAD + block("coil", 0, [True, 0, 1, False]))
    assert sc.devices[0].blocks[0].values == (1, 0, 1, 0)


def test_the_error_names_the_file(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text('[[device]]\nname = "a"\nkind = "nope"\n')
    with pytest.raises(ScenarioError, match=f"{path}: device 'a': unknown kind 'nope'"):
        load(path)


def test_a_missing_file_a_directory_and_binary_content_are_scenario_errors(tmp_path):
    with pytest.raises(ScenarioError, match="No such file"):
        load(tmp_path / "nothing.toml")
    with pytest.raises(ScenarioError):
        load(tmp_path)
    binary = tmp_path / "b.toml"
    binary.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ScenarioError, match="not UTF-8"):
        load(binary)


def test_a_huge_file_is_refused_before_it_is_read(tmp_path):
    big = tmp_path / "big.toml"
    big.write_text("# " + "x" * (1 << 20) + "\n")
    with pytest.raises(ScenarioError, match="too large"):
        load(big)


@pytest.mark.parametrize("name", ["devices", "DEVICES", "require_tools"])
def test_names_that_would_clash_with_other_variables_are_refused(name):
    with pytest.raises(ScenarioError, match="has another meaning"):
        parse(f'[[device]]\nname = "{name}"\nkind = "echo"\n')


def test_environment_names():
    assert env_name("meter") == "SERIAL_SIM_METER"
    assert env_name("gps-1") == "SERIAL_SIM_GPS_1"


def test_sections_list_every_key_the_checks_know():
    keys = {name: {k.name for k in keys} for name, keys in SECTIONS.items()}
    assert {"seed", "device"} <= keys["top level"]
    assert {"name", "kind", "link", "baud", "char_bits", "fault"} == keys["[[device]], every kind"]


# ---- whatever the file says, the answer is a Scenario or a ScenarioError ----

SEEDS = [
    (Path(__file__).resolve().parent.parent / "examples" / "demo" / "meter.toml").read_text(),
    MODBUS_HEAD + block("holding", 0, [1, 2]) + '[[device.fault]]\nkind = "delay"\nms = 5\nevery = 2\n',
    '[[device]]\nname = "c"\nkind = "script"\ninitial = "a"\n[device.prompts]\na = "> "\n'
    '[[device.rule]]\nmatch = "^x(.)$"\nsend = "${1}"\nnext = "a"\n',
    '[[device]]\nname = "s"\nkind = "stream"\nlines = ["a"]\ninterval_ms = 10\n',
]


@pytest.mark.parametrize("seed", range(150))
def test_mutated_scenarios_never_raise_anything_but_scenario_error(seed):
    rng = random.Random(seed)
    text = rng.choice(SEEDS)
    chars = list(text)
    for _ in range(rng.randrange(1, 6)):
        i = rng.randrange(len(chars))
        op = rng.choice("dir")
        if op == "d":
            del chars[i]
        elif op == "i":
            chars.insert(i, rng.choice('=[]"\\\n0-9a{}.,# '))
        else:
            chars[i] = rng.choice('=[]"\\\n09a{}.,# ')
        if not chars:
            break
    try:
        result = parse("".join(chars))
    except ScenarioError:
        return
    assert isinstance(result, Scenario)


@pytest.mark.parametrize("unit", [0, 248, 1000])
def test_the_unit_must_be_a_slave_address(unit):
    text = f'[[device]]\nname = "m"\nkind = "modbus-rtu"\nunit = {unit}\n' + block("holding", 0, [1])
    with pytest.raises(ScenarioError, match="unit must be between 1 and 247"):
        parse(text)


@pytest.mark.parametrize("unit", [1, 17, 247])
def test_the_edges_of_the_unit_range_are_fine(unit):
    text = f'[[device]]\nname = "m"\nkind = "modbus-rtu"\nunit = {unit}\n' + block("holding", 0, [1])
    assert parse(text).devices[0].unit == unit
