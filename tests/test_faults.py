import random

import pytest

from serial_sim import faults
from serial_sim.crc import crc_ok
from serial_sim.exchange import Emission, Exchange
from serial_sim.scenario import ScenarioError

from .util import MODBUS_HEAD, block, req, slave, spec_of


def fault_specs(*tables: str):
    text = MODBUS_HEAD + block("holding", 0, [1]) + "".join(f"[[device.fault]]\n{t}\n" for t in tables)
    return spec_of(text).faults


def pick(specs, numbers, function=3, seed=1):
    rng = random.Random(seed)
    return [n for n in numbers if faults.selected(specs, n, function, rng)]


# ---- which requests are hit ----


def test_every_hits_every_nth_request_counting_from_one():
    specs = fault_specs('kind = "drop"\nevery = 3')
    assert pick(specs, range(1, 13)) == [3, 6, 9, 12]


def test_after_and_until_make_a_window():
    specs = fault_specs('kind = "drop"\nafter = 2\nuntil = 5')
    assert pick(specs, range(1, 10)) == [3, 4, 5]


def test_a_fault_without_a_selector_hits_everything():
    assert pick(fault_specs('kind = "drop"'), range(1, 5)) == [1, 2, 3, 4]


def test_functions_limit_a_fault_to_some_requests():
    specs = fault_specs('kind = "drop"\nfunctions = [3, 4]')
    rng = random.Random(1)
    assert faults.selected(specs, 1, 3, rng) and faults.selected(specs, 1, 4, rng)
    assert not faults.selected(specs, 1, 6, rng)
    assert not faults.selected(specs, 1, None, rng)


def test_every_and_a_window_must_both_fit():
    specs = fault_specs('kind = "drop"\nevery = 2\nafter = 4')
    assert pick(specs, range(1, 13)) == [6, 8, 10, 12]


def test_probability_is_reproducible_for_one_seed_and_differs_for_another():
    specs = fault_specs('kind = "drop"\nprobability = 0.3')
    a = pick(specs, range(1, 201), seed=5)
    assert a == pick(specs, range(1, 201), seed=5)
    assert a != pick(specs, range(1, 201), seed=6)
    assert 30 <= len(a) <= 90  # about 30 % of 200, with a wide margin


def test_probability_one_hits_everything_and_the_generator_is_only_used_for_requests_that_pass_the_rest():
    specs = fault_specs('kind = "drop"\nprobability = 1\nevery = 2')
    rng = random.Random(3)
    state = rng.getstate()
    assert not faults.selected(specs, 1, 3, rng)
    assert rng.getstate() == state  # request 1 failed `every`, so nothing was drawn
    assert faults.selected(specs, 2, 3, rng)


def test_protocol_faults_are_applied_before_byte_level_ones():
    specs = fault_specs('kind = "truncate"\nkeep = 3', 'kind = "corrupt-crc"')
    chosen = faults.selected(specs, 1, 3, random.Random(1))
    assert [s.kind for s in chosen] == ["corrupt-crc", "truncate"]


# ---- what a fault does to the bytes ----

DATA = bytes(range(10, 30))


def one(kind_table: str, data: bytes = DATA, seed: int = 1):
    (spec,) = fault_specs(kind_table)
    return faults.apply_generic(spec, [Emission(0.05, data)], random.Random(seed))


def test_drop_removes_the_reply():
    assert one('kind = "drop"') == []


def test_delay_adds_to_the_first_part_only():
    (spec,) = fault_specs('kind = "delay"\nms = 250')
    out = faults.apply_generic(spec, [Emission(0.05, b"a"), Emission(0.01, b"b")], random.Random(1))
    assert [(e.delay, e.data) for e in out] == [(pytest.approx(0.30), b"a"), (0.01, b"b")]


@pytest.mark.parametrize("bits", [1, 2, 7, 64])
def test_corrupt_flips_exactly_that_many_bits(bits):
    (out,) = one(f'kind = "corrupt"\nbits = {bits}')
    flipped = sum(bin(a ^ b).count("1") for a, b in zip(out.data, DATA, strict=True))
    assert flipped == bits and len(out.data) == len(DATA)


def test_corrupt_cannot_flip_more_bits_than_there_are():
    (out,) = one('kind = "corrupt"\nbits = 64', data=b"\x00")
    assert out.data == b"\xff"


def test_corrupt_reaches_every_part_of_a_reply_in_several_parts():
    (spec,) = fault_specs('kind = "corrupt"\nbits = 64')
    out = faults.apply_generic(spec, [Emission(0, b"\x00\x00"), Emission(0, b"\x00\x00")], random.Random(1))
    assert [e.data for e in out] == [b"\xff\xff", b"\xff\xff"]


def test_garbage_puts_noise_in_front_and_leaves_the_reply_alone():
    (out,) = one('kind = "garbage"\ncount = 5')
    assert len(out.data) == len(DATA) + 5 and out.data.endswith(DATA)
    assert out.delay == 0.05


def test_the_noise_depends_on_the_seed_only():
    assert one('kind = "garbage"', seed=1) == one('kind = "garbage"', seed=1)
    assert one('kind = "garbage"', seed=1) != one('kind = "garbage"', seed=2)


def test_truncate_keeps_the_first_bytes_by_default_half():
    assert one('kind = "truncate"\nkeep = 4')[0].data == DATA[:4]
    assert one('kind = "truncate"')[0].data == DATA[:10]
    assert one('kind = "truncate"\nkeep = 0') == []
    assert one('kind = "truncate"\nkeep = 999')[0].data == DATA


def test_truncate_cuts_across_parts():
    (spec,) = fault_specs('kind = "truncate"\nkeep = 3')
    out = faults.apply_generic(
        spec, [Emission(0, b"ab"), Emission(0.1, b"cd"), Emission(0.1, b"ef")], random.Random(1)
    )
    assert [e.data for e in out] == [b"ab", b"c"]


def test_split_cuts_the_reply_in_two_with_a_pause():
    out = one('kind = "split"\nms = 40\nat = 6')
    assert [e.data for e in out] == [DATA[:6], DATA[6:]]
    assert [e.delay for e in out] == [0.05, pytest.approx(0.04)]
    assert [e.data for e in one('kind = "split"')] == [DATA[:10], DATA[10:]]


def test_split_outside_the_reply_does_nothing():
    assert [e.data for e in one('kind = "split"\nat = 500')] == [DATA]


def test_duplicate_sends_the_reply_twice_back_to_back():
    out = one('kind = "duplicate"')
    assert [e.data for e in out] == [DATA, DATA]
    assert [e.delay for e in out] == [0.05, 0.0]


def test_nothing_to_break_stays_nothing():
    (spec,) = fault_specs('kind = "corrupt"')
    assert faults.apply_generic(spec, [], random.Random(1)) == []


# ---- the Modbus faults ----


def exchange_for(pdu: str = "03 0000 0001"):
    s = slave(block("holding", 0, [1]))
    (ex,) = s.feed(req(pdu), 0.0)
    return s, ex


def test_corrupt_crc_leaves_everything_but_the_checksum_intact():
    s, ex = exchange_for()
    good = ex.emissions[0].data
    (spec,) = fault_specs('kind = "corrupt-crc"')
    out = s.protocol_fault(spec, ex).emissions[0].data
    assert out[:-1] == good[:-1] and out != good and not crc_ok(out)


def test_exception_replaces_the_answer_with_that_code():
    s, ex = exchange_for("06 0000 0005")
    (spec,) = fault_specs('kind = "exception"\ncode = 6')
    assert s.protocol_fault(spec, ex).emissions[0].data == req("86 06")
    assert s.data["holding"][0] == 5  # the write itself still happened: only the reply is faked


def test_wrong_unit_answers_as_someone_else_with_a_good_checksum():
    s, ex = exchange_for()
    (spec,) = fault_specs('kind = "wrong-unit"\nunit = 3')
    out = s.protocol_fault(spec, ex).emissions[0].data
    assert out[0] == 3 and crc_ok(out) and out[1:-2] == ex.emissions[0].data[1:-2]


def test_apply_runs_the_chain_and_a_drop_ends_it():
    s, ex = exchange_for()
    specs = fault_specs('kind = "delay"\nms = 10', 'kind = "drop"', 'kind = "corrupt"')
    out = faults.apply(ex, specs, s, random.Random(1))
    assert out.emissions == [] and out.notes == ["delay", "drop"]


def test_apply_on_an_exchange_without_a_reply_does_nothing():
    s, _ = exchange_for()
    ex = Exchange(request=b"x", emissions=[])
    assert faults.apply(ex, fault_specs('kind = "drop"'), s, random.Random(1)).notes == []


# ---- the scenario checks that belong to faults ----


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ('kind = "dorp"', "did you mean 'drop'"),
        ('kind = "delay"', "needs ms"),
        ('kind = "drop"\nms = 5', "ms does not apply to the fault 'drop'"),
        ('kind = "exception"', "needs code"),
        ('kind = "exception"\ncode = 0', "code must be between 1 and 255"),
        ('kind = "wrong-unit"', "needs unit"),
        ('kind = "drop"\nevery = 0', "every must be between 1"),
        ('kind = "drop"\nprobability = 0', "probability must be above 0"),
        ('kind = "drop"\nprobability = 1.5', "probability must be above 0"),
        ('kind = "drop"\nafter = 5\nuntil = 5', "must be larger than after"),
        ('kind = "drop"\nfunctions = [0]', "functions must be between 1 and 127"),
        ('kind = "corrupt"\nbits = 0', "bits must be between 1 and 64"),
        ('kind = "split"\nat = 0', "at must be between 1"),
        ('kind = "garbage"\ncount = 0', "count must be between 1"),
        ('kind = "delay"\nms = 99999999', "ms must be between 0 and 60000"),
        ('kind = "drop"\nevery = "3"', "every must be an int"),
    ],
)
def test_a_bad_fault_is_refused_with_a_message_that_says_why(table, message):
    with pytest.raises(ScenarioError, match=message):
        fault_specs(table)


def test_modbus_faults_are_refused_on_other_kinds():
    text = '[[device]]\nname = "x"\nkind = "echo"\n[[device.fault]]\nkind = "corrupt-crc"\n'
    with pytest.raises(ScenarioError, match="only exists for modbus-rtu"):
        spec_of(text)
    text = '[[device]]\nname = "x"\nkind = "echo"\n[[device.fault]]\nkind = "drop"\nfunctions = [3]\n'
    with pytest.raises(ScenarioError, match="functions only applies to modbus-rtu"):
        spec_of(text)
