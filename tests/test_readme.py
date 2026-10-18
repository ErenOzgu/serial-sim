"""The README is checked against the code and the files it shows, so that it cannot drift from them."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from serial_sim import cli
from serial_sim.docs import faults_markdown, functions_markdown, reference_markdown
from serial_sim.scenario import RESERVED_ENV

from .util import require_or_skip, tool_missing

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")

INCLUDED = {
    "examples/demo/transcript.txt",
    "examples/demo/meter.toml",
    "examples/flaky-meter.toml",
    "examples/pytest/conftest.py",
}


def between(start: str, end: str) -> str:
    match = re.search(rf"<!-- {start} -->\n(.*?)\n<!-- {end} -->", README, re.S)
    assert match, f"no {start} .. {end} markers in the README"
    return match.group(1)


def section(title: str) -> str:
    match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", README, re.S | re.M)
    assert match, f"no section {title!r}"
    return match.group(1)


# ---- files shown in the README are the files in the repository ----


def test_included_blocks_are_the_files():
    blocks = re.findall(r"<!-- include:(\S+) -->\n```\w*\n(.*?)\n?```", README, re.S)
    assert {path for path, _ in blocks} == INCLUDED
    for path, body in blocks:
        assert body == (ROOT / path).read_text(encoding="utf-8").rstrip("\n"), f"{path} differs"


def test_generated_tables_are_what_the_code_prints_today():
    assert between("reference:start", "reference:end") == reference_markdown().strip("\n")
    assert between("functions:start", "functions:end") == functions_markdown().strip("\n")
    assert between("faults:start", "faults:end") == faults_markdown().strip("\n")


def test_every_path_the_readme_names_exists():
    named = set(re.findall(r"`((?:examples|docs|serial_sim|tests)/[\w./-]+)`", README))
    named |= set(re.findall(r"\]\(((?!http)[\w./-]+)\)", README))
    named |= set(re.findall(r'src="([\w./-]+)"', README))
    assert {"docs/banner.svg", "LICENSE", "examples/demo/ask.py", "examples/ci/serial-tests.yml"} <= named
    for path in sorted(named):
        assert (ROOT / path.rstrip("/")).exists(), path


def test_the_layout_lists_real_modules():
    layout = section("Layout")
    for name in re.findall(r"\b([a-z_]+\.py)\b", layout):
        assert (ROOT / "serial_sim" / name).exists(), name


def test_badges_agree_with_the_package():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.11"' in pyproject and "python-3.11%2B" in README
    assert "dependencies = []" in pyproject and "dependencies-none" in README
    assert (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")


# ---- the command line described is the command line there is ----


def parsers() -> dict[str, argparse.ArgumentParser]:
    top = cli.build_parser()
    subs = next(a for a in top._actions if isinstance(a, argparse._SubParsersAction))
    return {"": top, **subs.choices}


def test_every_command_and_option_is_documented():
    commands = section("Commands")
    for name, parser in parsers().items():
        if name:
            assert f"`serial-sim {name}" in commands, f"command {name} is not documented"
        for action in parser._actions:
            for option in action.option_strings:
                if option in ("-h", "--help"):
                    continue
                assert f"`{option}" in commands, (
                    f"option {option} of {name or 'serial-sim'} is not documented"
                )


def test_the_documented_options_exist():
    known = {o for p in parsers().values() for a in p._actions for o in a.option_strings}
    commands = section("Commands")
    documented = set(re.findall(r"`(--?[a-z][\w-]*)", commands))
    assert documented <= known, sorted(documented - known)


def test_exit_statuses_are_the_ones_the_program_uses():
    table = section("Commands").split("| Exit status |")[1]
    documented = set(re.findall(r"^\| (\d+) \|", table, re.M))
    assert documented == {
        str(code)
        for code in (
            cli.EXIT_OK,
            cli.EXIT_USAGE,
            cli.EXIT_ENV,
            cli.EXIT_SIMULATOR,
            cli.EXIT_COMMAND_NOT_FOUND,
        )
    }
    assert "| 128 + n |" in table


def test_the_reserved_names_are_documented():
    for name in RESERVED_ENV:
        assert f"`{name}`" in README


def test_the_commands_in_try_it_run(tmp_path):
    """The shell lines of "Try it" that start with serial-sim are run, with the installed command faked."""
    require_or_skip(not tool_missing("mbpoll"), "mbpoll")
    block = re.search(r"```sh\n(.*?)```", section("Try it"), re.S)
    assert block
    text = block.group(1).replace("\\\n", " ")
    commands = [line.strip() for line in text.splitlines() if line.strip().startswith("serial-sim")]
    assert len(commands) == 2
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "serial-sim"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m serial_sim "$@"\n')
    shim.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    (work / "examples").symlink_to(ROOT / "examples")
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin:/usr/local/bin", "PYTHONPATH": str(ROOT), "HOME": str(tmp_path)}
    outputs = []
    for command in commands:
        done = subprocess.run(
            ["bash", "-c", command],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        outputs.append(done.stdout)
    for value in ("2301", "512", "40"):
        assert re.search(rf"^\[\d\]:\s+{value}$", outputs[0], re.M), outputs[0]
    assert "11 03 00 00 00 03 07 5b" in outputs[1]
