import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from serial_sim import __version__
from serial_sim.cli import main

from .util import MODBUS_HEAD, Port, block, req

ROOT = Path(__file__).resolve().parent.parent
METER = ROOT / "examples" / "demo" / "meter.toml"
ECHO = '[[device]]\nname = "e"\nkind = "echo"\n'


def run_cli(*args: str, timeout: float = 30, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "serial_sim", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=ROOT,
        check=False,
        **kw,
    )


def scenario(tmp_path: Path, text: str) -> str:
    path = tmp_path / "s.toml"
    path.write_text(text)
    return str(path)


# ---- validate and show ----


def test_validate_describes_the_scenario(capsys):
    assert main(["validate", str(METER)]) == 0
    out = capsys.readouterr().out
    assert "1 device(s), seed 1" in out
    assert "meter" in out and "modbus-rtu; unit 17, 3 block(s); 9600 baud" in out


def test_validate_says_when_a_device_sends_its_characters_one_by_one(tmp_path, capsys):
    path = scenario(tmp_path, ECHO + "baud = 9600\nbytewise = true\n")
    assert main(["validate", path]) == 0
    assert "9600 baud, bytewise" in capsys.readouterr().out
    (tmp_path / "plain.toml").write_text(ECHO + "baud = 9600\n")
    assert main(["validate", str(tmp_path / "plain.toml")]) == 0
    assert "bytewise" not in capsys.readouterr().out


def test_validate_lists_the_faults(tmp_path, capsys):
    path = scenario(
        tmp_path, ECHO + '[[device.fault]]\nkind = "drop"\n[[device.fault]]\nkind = "delay"\nms = 1\n'
    )
    assert main(["validate", path]) == 0
    assert "faults: drop, delay" in capsys.readouterr().out


@pytest.mark.parametrize("text", ["", "[[device]]\nname = 'a'\n", "not toml ["])
def test_a_bad_scenario_is_exit_2_with_the_reason_on_stderr(tmp_path, capsys, text):
    assert main(["validate", scenario(tmp_path, text)]) == 2
    assert capsys.readouterr().err.startswith("serial-sim: ")


def test_a_missing_scenario_is_exit_2(capsys):
    assert main(["validate", "/no/such/file.toml"]) == 2
    assert "No such file" in capsys.readouterr().err


def write_log(path: Path) -> None:
    lines = [
        {"t": 0.0, "device": "a", "dir": "rx", "hex": "68 69 0d", "text": "hi\\r"},
        {"t": 0.5, "device": "a", "dir": "tx", "hex": "68 69", "text": "hi"},
        {"t": 0.75, "device": "b", "event": "fault drop on request 2"},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in lines))


def test_show_prints_the_transcript(tmp_path, capsys):
    log = tmp_path / "t.jsonl"
    write_log(log)
    assert main(["show", str(log)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "   0.000  a          rx  68 69 0d",
        "   0.500  a          tx  68 69",
        "   0.750  b          --  fault drop on request 2",
    ]


def test_show_can_filter_hide_times_and_add_text(tmp_path, capsys):
    log = tmp_path / "t.jsonl"
    write_log(log)
    assert main(["show", str(log), "--device", "a", "--no-time", "--text"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "a          rx  68 69 0d   |hi\\r|",
        "a          tx  68 69   |hi|",
    ]


def test_show_refuses_a_file_that_is_not_a_transcript(tmp_path, capsys):
    bad = tmp_path / "x"
    bad.write_text('{"nothing": 1}\n')
    assert main(["show", str(bad)]) == 2
    assert "not a serial-sim transcript line" in capsys.readouterr().err
    bad.write_text("garbage\n")
    assert main(["show", str(bad)]) == 2
    assert "not JSON" in capsys.readouterr().err
    assert main(["show", str(tmp_path / "missing")]) == 2


def test_version():
    r = run_cli("--version")
    assert r.returncode == 0 and r.stdout.strip() == f"serial-sim {__version__}"


# ---- run ----


def wait_for(path: Path, timeout: float = 10) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists():
            return json.loads(path.read_text())
        time.sleep(0.05)
    raise AssertionError(f"{path} did not appear")


def start_run(tmp_path: Path, *extra: str) -> tuple[subprocess.Popen, dict, Path]:
    ready = tmp_path / "ready.json"
    proc = subprocess.Popen(
        [sys.executable, "-m", "serial_sim", "run", str(METER), "--ready-file", str(ready), *extra],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=ROOT,
    )
    try:
        info = wait_for(ready)
    except AssertionError:
        proc.kill()
        raise
    return proc, info, ready


def test_run_serves_until_the_duration_ends_and_cleans_up(tmp_path):
    proc, info, ready = start_run(tmp_path, "--duration", "1.5")
    link = info["devices"]["meter"]
    assert info["pid"] == proc.pid
    with Port(link) as port:
        port.write(req("03 0000 0003"))
        assert port.read(11) == req("03 06 08fd02000028")
    out, err = proc.communicate(timeout=15)
    assert proc.returncode == 0, err
    assert "meter" in out and link in out and "modbus-rtu" in out
    assert not os.path.lexists(link) and not ready.exists()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_run_stops_cleanly_on_a_signal(tmp_path, sig):
    proc, info, ready = start_run(tmp_path, "-q")
    link = info["devices"]["meter"]
    proc.send_signal(sig)
    out, err = proc.communicate(timeout=10)
    assert proc.returncode == 0, err
    assert out == ""  # -q: no table
    assert not os.path.lexists(link) and not ready.exists()


def test_run_with_a_log_writes_the_transcript(tmp_path):
    log = tmp_path / "t.jsonl"
    proc, info, _ = start_run(tmp_path, "--log", str(log), "-q")
    with Port(info["devices"]["meter"]) as port:
        port.write(req("03 0000 0001"))
        port.read(7)
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=10)
    assert [r["dir"] for r in map(json.loads, log.read_text().splitlines()) if "dir" in r] == ["rx", "tx"]


def test_run_uses_the_link_and_the_directory_it_is_given(tmp_path):
    link = tmp_path / "mine"
    proc, info, _ = start_run(tmp_path, "--link", f"meter={link}", "-q")
    assert info["devices"]["meter"] == str(link) and link.is_symlink()
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=10)
    scen = scenario(tmp_path, ECHO)
    proc2 = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "serial_sim",
            "run",
            scen,
            "--dir",
            str(tmp_path),
            "--ready-file",
            str(tmp_path / "r2"),
        ],
        stdout=subprocess.PIPE,
        text=True,
        cwd=ROOT,
    )
    info2 = wait_for(tmp_path / "r2")
    assert info2["devices"]["e"] == str(tmp_path / "e")
    proc2.send_signal(signal.SIGTERM)
    proc2.communicate(timeout=10)


# ---- environment and usage errors ----


def test_a_link_for_a_device_that_does_not_exist_is_a_usage_error(tmp_path, capsys):
    assert main(["run", str(METER), "--link", "nope=/tmp/x", "--duration", "1"]) == 2
    assert "names no device" in capsys.readouterr().err


def test_a_link_that_cannot_be_made_is_an_environment_error(tmp_path, capsys):
    assert main(["run", str(METER), "--link", f"meter={tmp_path}/no/such/dir/x", "--duration", "1"]) == 3
    assert "cannot create the link" in capsys.readouterr().err


def test_a_link_in_the_way_is_an_environment_error_and_the_file_is_untouched(tmp_path, capsys):
    (tmp_path / "meter").write_text("precious")
    assert main(["run", str(METER), "--dir", str(tmp_path), "--duration", "1", "-q"]) == 3
    assert "is not a symbolic link" in capsys.readouterr().err
    assert (tmp_path / "meter").read_text() == "precious"


def test_a_log_that_cannot_be_written_is_an_environment_error(tmp_path, capsys):
    assert main(["run", str(METER), "--log", str(tmp_path / "no" / "t.jsonl"), "--duration", "1"]) == 3
    assert "No such file" in capsys.readouterr().err


def test_a_ready_file_that_cannot_be_written_is_an_environment_error_and_nothing_is_left(tmp_path, capsys):
    assert (
        main(
            [
                "run",
                str(METER),
                "--dir",
                str(tmp_path),
                "--ready-file",
                str(tmp_path / "no" / "r"),
                "--duration",
                "1",
                "-q",
            ]
        )
        == 3
    )
    assert not os.path.lexists(tmp_path / "meter")


@pytest.mark.parametrize("value", ["0", "-1", "soon"])
def test_a_bad_duration_is_refused_by_the_parser(value):
    r = run_cli("run", str(METER), "--duration", value)
    assert r.returncode == 2 and "duration" in r.stderr


def test_a_link_option_without_an_equals_sign_is_refused():
    r = run_cli("run", str(METER), "--link", "meter")
    assert r.returncode == 2 and "NAME=PATH" in r.stderr


# ---- exec ----

CHILD = textwrap.dedent(
    """
    import json, os, sys
    print(json.dumps({
        "meter": os.environ["SERIAL_SIM_METER"],
        "devices": json.loads(os.environ["SERIAL_SIM_DEVICES"]),
        "exists": os.path.exists(os.environ["SERIAL_SIM_METER"]),
    }))
    sys.exit(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
    """
)


def test_exec_gives_the_command_the_link_and_removes_it_afterwards(tmp_path):
    r = run_cli("exec", str(METER), "--", sys.executable, "-c", CHILD)
    assert r.returncode == 0, r.stderr
    seen = json.loads(r.stdout)
    assert seen["exists"] and seen["devices"] == {"meter": seen["meter"]}
    assert not os.path.lexists(seen["meter"])


@pytest.mark.parametrize("code", [1, 3, 42])
def test_exec_exits_with_the_status_of_the_command(code):
    r = run_cli("exec", str(METER), "--", sys.executable, "-c", CHILD, str(code))
    assert r.returncode == code


def test_exec_reports_a_command_killed_by_a_signal_like_a_shell_does():
    r = run_cli(
        "exec",
        str(METER),
        "--",
        sys.executable,
        "-c",
        "import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
    )
    assert r.returncode == 128 + 9


def test_exec_with_a_command_that_does_not_exist_is_127():
    r = run_cli("exec", str(METER), "--", "/no/such/command")
    assert r.returncode == 127 and "cannot run /no/such/command" in r.stderr


def test_exec_without_a_command_is_a_usage_error():
    for argv in (["exec", str(METER)], ["exec", str(METER), "--"]):
        r = run_cli(*argv)
        assert r.returncode == 2 and "needs a command" in r.stderr


def test_options_after_the_command_belong_to_the_command(tmp_path):
    log = tmp_path / "t.jsonl"
    r = run_cli(
        "exec",
        str(METER),
        "--log",
        str(log),
        "--",
        sys.executable,
        "-c",
        "import sys; print(sys.argv[1:])",
        "--log",
        "x",
    )
    assert r.returncode == 0 and r.stdout.strip() == "['--log', 'x']"
    assert log.exists()


def test_exec_runs_a_real_client_and_the_transcript_matches(tmp_path):
    client = textwrap.dedent(
        f"""
        import os, sys
        sys.path.insert(0, {str(ROOT)!r})
        from tests.util import Port, req
        with Port(os.environ["SERIAL_SIM_METER"]) as port:
            port.write(req("03 0000 0002"))
            got = port.read(9)
        sys.exit(0 if got == req("03 04 08fd0200") else 5)
        """
    )
    log = tmp_path / "t.jsonl"
    r = run_cli("exec", str(METER), "--log", str(log), "--", sys.executable, "-c", client)
    assert r.returncode == 0, r.stderr
    shown = run_cli("show", str(log), "--no-time")
    assert shown.stdout.splitlines() == [
        "meter      rx  " + req("03 0000 0002").hex(" "),
        "meter      tx  " + req("03 04 08fd0200").hex(" "),
    ]


def test_exec_names_every_device_in_the_environment(tmp_path):
    scen = scenario(
        tmp_path, ECHO + '[[device]]\nname = "gps-1"\nkind = "stream"\nlines = ["x"]\ninterval_ms = 100\n'
    )
    code = "import json, os; print(json.dumps({k: v for k, v in os.environ.items() if k.startswith('SERIAL_SIM_')}, sort_keys=True))"
    r = run_cli("exec", scen, "--", sys.executable, "-c", code)
    env = json.loads(r.stdout)
    env.pop("SERIAL_SIM_REQUIRE_TOOLS", None)  # set by the test run itself, not by `exec`
    assert sorted(env) == ["SERIAL_SIM_DEVICES", "SERIAL_SIM_E", "SERIAL_SIM_GPS_1"]
    assert json.loads(env["SERIAL_SIM_DEVICES"]) == {
        "e": env["SERIAL_SIM_E"],
        "gps-1": env["SERIAL_SIM_GPS_1"],
    }


def test_exec_forwards_sigterm_to_the_command_and_cleans_up(tmp_path):
    script = "import os, sys, time; print(os.environ['SERIAL_SIM_METER'], flush=True); time.sleep(60)"
    proc = subprocess.Popen(
        [sys.executable, "-m", "serial_sim", "exec", str(METER), "--", sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        text=True,
        cwd=ROOT,
    )
    link = proc.stdout.readline().strip()
    assert os.path.lexists(link)
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)
    assert proc.returncode == 128 + 15
    assert not os.path.lexists(link)


def test_exec_says_so_when_the_simulator_died_while_the_command_ran(tmp_path, monkeypatch, capsys):
    import serial_sim.engine as engine

    def broken(self, should_stop, max_poll=0.02):
        raise RuntimeError("boom")

    monkeypatch.setattr(engine.Engine, "serve", broken)
    code = main(["exec", str(METER), "--", sys.executable, "-c", "import time; time.sleep(0.2)"])
    assert code == 4
    assert "the simulator stopped with an error" in capsys.readouterr().err


def test_a_scenario_error_stops_exec_before_anything_starts(tmp_path, capsys):
    assert main(["exec", scenario(tmp_path, "nonsense"), "--", "true"]) == 2


def test_exec_uses_a_modbus_head_scenario_too(tmp_path):
    scen = scenario(tmp_path, MODBUS_HEAD + block("holding", 0, [5]))
    r = run_cli(
        "exec",
        scen,
        "--",
        sys.executable,
        "-c",
        "import os; print(os.path.basename(os.environ['SERIAL_SIM_M']))",
    )
    assert r.stdout.strip() == "m"


@pytest.mark.parametrize("value", ["=x", "meter=", "="])
def test_a_link_needs_both_a_name_and_a_path(value):
    r = run_cli("run", str(METER), "--link", value, timeout=10)
    assert r.returncode == 2 and "NAME=PATH" in r.stderr
