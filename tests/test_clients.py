"""The simulator against software written by other people: pymodbus, mbpoll and pyserial.

If the Modbus frames were wrong, or the port behaved unlike a serial port, these would fail where tests written by
the same hand as the simulator would not. In CI the tools are installed and a missing one is a failure
(SERIAL_SIM_REQUIRE_TOOLS=1); elsewhere the tests that need them are skipped.
"""

from __future__ import annotations

import inspect
import re
import subprocess
import time

import pytest

from .util import MODBUS_HEAD, block, require_or_skip, running, tool_missing

METER = (
    MODBUS_HEAD
    + block("holding", 0, [2301, 512, 40])
    + block("holding", 10, [7, 8], "writable = false")
    + block("input", 0, [1000, 2000], "step = 1")
    + block("coil", 0, [True, False, False, True, True, False, True, False, True, True])
    + block("discrete", 0, [False, True, True])
)

try:
    from pymodbus.client import ModbusSerialClient
except ImportError:  # pragma: no cover - dev dependency
    ModbusSerialClient = None


def need_pymodbus():
    require_or_skip(ModbusSerialClient is not None, "pymodbus")


class Failed:
    """What a pymodbus call that raised looks like to the tests: an error without a payload."""

    registers: list = []

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def isError(self) -> bool:  # noqa: N802 - pymodbus spelling
        return True


def safe(call, *args, **kw):
    """pymodbus raises when no usable reply arrives and returns an error object for an exception reply."""
    from pymodbus.exceptions import ModbusException

    try:
        return call(*args, **kw)
    except ModbusException as exc:
        return Failed(exc)


def unit_kw(unit: int) -> dict:
    """pymodbus renamed `slave` to `device_id`; accept whichever this version has."""
    params = inspect.signature(ModbusSerialClient.read_holding_registers).parameters
    return {"device_id": unit} if "device_id" in params else {"slave": unit}


@pytest.fixture
def meter():
    need_pymodbus()
    with running(METER) as (paths, engine):
        client = ModbusSerialClient(paths["m"], baudrate=9600, timeout=1, retries=0)
        assert client.connect()
        try:
            yield client, engine
        finally:
            client.close()


# ---- pymodbus ----


def test_pymodbus_reads_every_kind_of_data(meter):
    c, _ = meter
    kw = unit_kw(17)
    assert c.read_holding_registers(0, count=3, **kw).registers == [2301, 512, 40]
    assert c.read_input_registers(0, count=2, **kw).registers == [1000, 2000]
    assert c.read_coils(0, count=10, **kw).bits[:10] == [
        True,
        False,
        False,
        True,
        True,
        False,
        True,
        False,
        True,
        True,
    ]
    assert c.read_discrete_inputs(0, count=3, **kw).bits[:3] == [False, True, True]


def test_pymodbus_writes_and_reads_back(meter):
    c, _ = meter
    kw = unit_kw(17)
    assert not c.write_register(2, 99, **kw).isError()
    assert c.read_holding_registers(2, count=1, **kw).registers == [99]
    assert not c.write_registers(0, [11, 22], **kw).isError()
    assert c.read_holding_registers(0, count=3, **kw).registers == [11, 22, 99]
    assert not c.write_coil(1, True, **kw).isError()
    assert c.read_coils(1, count=1, **kw).bits[0] is True
    assert not c.write_coils(5, [True, True, True], **kw).isError()
    assert c.read_coils(4, count=5, **kw).bits[:5] == [True, True, True, True, True]


def test_pymodbus_sees_exceptions(meter):
    c, _ = meter
    kw = unit_kw(17)
    r = c.read_holding_registers(100, count=1, **kw)
    assert r.isError() and r.exception_code == 2
    r = c.write_register(10, 1, **kw)  # a read-only block
    assert r.isError() and r.exception_code == 2
    assert c.read_holding_registers(10, count=2, **kw).registers == [7, 8]


def test_pymodbus_sees_the_input_registers_count_up(meter):
    c, _ = meter
    kw = unit_kw(17)
    first = c.read_input_registers(0, count=1, **kw).registers[0]
    assert [c.read_input_registers(0, count=1, **kw).registers[0] for _ in range(3)] == [
        first + 1,
        first + 2,
        first + 3,
    ]


def test_pymodbus_gets_no_answer_from_another_address(meter):
    c, _ = meter
    c.timeout = 0.3
    assert safe(c.read_holding_registers, 0, count=1, **unit_kw(18)).isError()


def faulty(fault: str):
    return running(MODBUS_HEAD + block("holding", 0, [1, 2, 3]) + f"[[device.fault]]\n{fault}\n")


def test_pymodbus_times_out_on_dropped_replies_and_recovers():
    need_pymodbus()
    with faulty('kind = "drop"\nevery = 2') as (paths, _):
        c = ModbusSerialClient(paths["m"], baudrate=9600, timeout=0.3, retries=0)
        assert c.connect()
        try:
            results = [safe(c.read_holding_registers, 0, count=3, **unit_kw(17)).isError() for _ in range(4)]
        finally:
            c.close()
    assert results == [False, True, False, True]


@pytest.mark.parametrize(
    "fault", ['kind = "corrupt-crc"', 'kind = "truncate"\nkeep = 4', 'kind = "wrong-unit"\nunit = 9']
)
def test_pymodbus_refuses_replies_that_are_broken(fault):
    need_pymodbus()
    with faulty(fault + "\nevery = 2") as (paths, _):
        c = ModbusSerialClient(paths["m"], baudrate=9600, timeout=0.3, retries=0)
        assert c.connect()
        try:
            results = [safe(c.read_holding_registers, 0, count=3, **unit_kw(17)) for _ in range(4)]
        finally:
            c.close()
    assert [r.isError() for r in results] == [False, True, False, True]
    assert results[0].registers == [1, 2, 3] and results[2].registers == [1, 2, 3]


def test_pymodbus_retries_get_through_a_flaky_line():
    need_pymodbus()
    with faulty('kind = "drop"\nevery = 2') as (paths, _):
        c = ModbusSerialClient(paths["m"], baudrate=9600, timeout=0.3, retries=2)
        assert c.connect()
        try:
            results = [safe(c.read_holding_registers, 0, count=3, **unit_kw(17)) for _ in range(4)]
        finally:
            c.close()
    assert not any(r.isError() for r in results)


def test_pymodbus_receives_the_exception_a_fault_injects():
    need_pymodbus()
    with faulty('kind = "exception"\ncode = 6') as (paths, _):
        c = ModbusSerialClient(paths["m"], baudrate=9600, timeout=0.5, retries=0)
        assert c.connect()
        try:
            r = c.read_holding_registers(0, count=3, **unit_kw(17))
        finally:
            c.close()
    assert r.isError() and r.exception_code == 6


def test_pymodbus_waits_for_a_delayed_reply():
    need_pymodbus()
    with faulty('kind = "delay"\nms = 300') as (paths, _):
        c = ModbusSerialClient(paths["m"], baudrate=9600, timeout=2, retries=0)
        assert c.connect()
        try:
            start = time.monotonic()
            r = c.read_holding_registers(0, count=3, **unit_kw(17))
            took = time.monotonic() - start
        finally:
            c.close()
    assert r.registers == [1, 2, 3] and 0.28 <= took < 1.5


def test_pymodbus_assembles_a_reply_that_comes_in_two_parts():
    need_pymodbus()
    with faulty('kind = "split"\nms = 80') as (paths, _):
        c = ModbusSerialClient(paths["m"], baudrate=9600, timeout=2, retries=0)
        assert c.connect()
        try:
            r = c.read_holding_registers(0, count=3, **unit_kw(17))
        finally:
            c.close()
    assert r.registers == [1, 2, 3]


# ---- mbpoll ----


def mbpoll(
    path: str, *args: str, write: tuple = (), unit: int = 17, timeout: float = 5
) -> subprocess.CompletedProcess:
    require_or_skip(not tool_missing("mbpoll"), "mbpoll")
    cmd = [
        "mbpoll",
        "-m",
        "rtu",
        "-a",
        str(unit),
        "-b",
        "9600",
        "-P",
        "none",
        "-1",
        "-o",
        "0.5",
        *args,
        path,
        *map(str, write),
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def values(out: str) -> list[int]:
    return [int(v) for v in re.findall(r"^\[\d+\]:\s+(\d+)\s*$", out, re.M)]


@pytest.fixture
def meter_path():
    with running(METER) as (paths, _):
        yield paths["m"]


def test_mbpoll_reads_holding_registers(meter_path):
    r = mbpoll(meter_path, "-t", "4", "-r", "1", "-c", "3")
    assert r.returncode == 0, r.stderr
    assert values(r.stdout) == [2301, 512, 40]


def test_mbpoll_reads_input_registers_that_count_up(meter_path):
    first = values(mbpoll(meter_path, "-t", "3", "-r", "1", "-c", "1").stdout)
    second = values(mbpoll(meter_path, "-t", "3", "-r", "1", "-c", "1").stdout)
    assert first == [1000] and second == [1001]


def test_mbpoll_reads_coils_and_discrete_inputs(meter_path):
    assert values(mbpoll(meter_path, "-t", "0", "-r", "1", "-c", "6").stdout) == [1, 0, 0, 1, 1, 0]
    assert values(mbpoll(meter_path, "-t", "1", "-r", "1", "-c", "3").stdout) == [0, 1, 1]


def test_mbpoll_writes_a_register_and_reads_it_back(meter_path):
    w = mbpoll(meter_path, "-t", "4", "-r", "3", write=(99,))
    assert w.returncode == 0 and "Written 1 references" in w.stdout
    assert values(mbpoll(meter_path, "-t", "4", "-r", "3", "-c", "1").stdout) == [99]


def test_mbpoll_writes_several_registers_and_coils(meter_path):
    assert mbpoll(meter_path, "-t", "4", "-r", "1", write=(5, 6, 7)).returncode == 0
    assert values(mbpoll(meter_path, "-t", "4", "-r", "1", "-c", "3").stdout) == [5, 6, 7]
    assert mbpoll(meter_path, "-t", "0", "-r", "2", write=(1, 1, 0)).returncode == 0
    assert values(mbpoll(meter_path, "-t", "0", "-r", "1", "-c", "4").stdout) == [1, 1, 1, 0]


def test_mbpoll_reports_an_exception(meter_path):
    r = mbpoll(meter_path, "-t", "4", "-r", "101", "-c", "1")
    assert r.returncode != 0 and "Illegal data address" in r.stderr
    r = mbpoll(meter_path, "-t", "4", "-r", "11", write=(1,))  # the read-only block
    assert r.returncode != 0 and "Illegal data address" in r.stderr


def test_mbpoll_times_out_on_another_address(meter_path):
    r = mbpoll(meter_path, "-t", "4", "-r", "1", "-c", "1", unit=18)
    assert r.returncode != 0 and "timed out" in r.stderr


def test_mbpoll_sees_the_injected_exception_and_the_dropped_reply():
    require_or_skip(not tool_missing("mbpoll"), "mbpoll")
    text = (
        MODBUS_HEAD + block("holding", 0, [1]) + '[[device.fault]]\nkind = "exception"\ncode = 4\nevery = 2\n'
    )
    with running(text) as (paths, _):
        ok = mbpoll(paths["m"], "-t", "4", "-r", "1", "-c", "1")
        bad = mbpoll(paths["m"], "-t", "4", "-r", "1", "-c", "1")
    assert ok.returncode == 0 and values(ok.stdout) == [1]
    assert bad.returncode != 0 and "Slave device or server failure" in bad.stderr


# ---- pyserial ----


def need_pyserial():
    try:
        import serial  # noqa: F401
    except ImportError:  # pragma: no cover - dev dependency
        require_or_skip(False, "pyserial")


def test_pyserial_talks_to_a_console():
    need_pyserial()
    import serial

    text = (
        '[[device]]\nname = "c"\nkind = "script"\ninitial = "login"\nbaud = 115200\n'
        '[device.prompts]\nlogin = "login: "\nshell = "# "\n'
        '[[device.rule]]\nstate = "login"\nmatch = "^root$"\nsend = "Welcome\\r\\n"\nnext = "shell"\n'
        '[[device.rule]]\nstate = "shell"\nmatch = "^date$"\nsend = "Thu Jan  1 00:00:00 UTC 1970\\r\\n"\n'
    )
    with running(text) as (paths, _):
        with serial.Serial(paths["c"], 115200, timeout=1) as port:
            time.sleep(
                0.2
            )  # the prompt sent at start is waiting in the buffer: throw it away, then ask again
            port.reset_input_buffer()
            port.write(b"\r")
            assert port.read_until(b"login: ") == b"login: "
            port.write(b"root\r")
            assert port.read_until(b"# ") == b"Welcome\r\n# "
            port.write(b"date\r")
            assert port.read_until(b"# ") == b"Thu Jan  1 00:00:00 UTC 1970\r\n# "


def test_pyserial_reads_a_stream_line_by_line():
    need_pyserial()
    import serial

    text = '[[device]]\nname = "g"\nkind = "stream"\nlines = ["$GPGGA,1", "$GPGGA,2"]\ninterval_ms = 30\n'
    with running(text) as (paths, _):
        with serial.Serial(paths["g"], 9600, timeout=1) as port:
            got = [port.readline() for _ in range(5)]
    assert all(g in (b"$GPGGA,1\r\n", b"$GPGGA,2\r\n") for g in got)
    assert all(a != b for a, b in zip(got, got[1:], strict=False))  # the two lines alternate, in order


def test_pyserial_times_out_when_the_device_stays_silent():
    need_pyserial()
    import serial

    text = '[[device]]\nname = "e"\nkind = "echo"\n[[device.fault]]\nkind = "drop"\n'
    with running(text) as (paths, _):
        with serial.Serial(paths["e"], 9600, timeout=0.3) as port:
            port.write(b"hello")
            assert port.read(5) == b""


def test_pyserial_applies_its_own_line_settings_without_the_simulator_minding():
    need_pyserial()
    import serial

    text = '[[device]]\nname = "e"\nkind = "echo"\n'
    with running(text) as (paths, _):
        for baud, parity in (
            (300, serial.PARITY_NONE),
            (115200, serial.PARITY_EVEN),
            (9600, serial.PARITY_ODD),
        ):
            with serial.Serial(paths["e"], baud, parity=parity, timeout=1) as port:
                port.write(b"ping")
                assert port.read(4) == b"ping"
