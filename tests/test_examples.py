"""The examples are real: they validate, they run, and the committed transcript is what the simulator prints."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from serial_sim.scenario import load

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = sorted(ROOT.glob("examples/**/*.toml"))


def test_there_are_example_scenarios():
    assert len(EXAMPLES) >= 4


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_example_scenario_is_valid(path):
    assert load(path).devices


def test_the_gps_sentences_have_correct_checksums():
    lines = re.findall(r'"(\$[^"]+)"', (ROOT / "examples" / "gps.toml").read_text(encoding="utf-8"))
    assert len(lines) == 3
    for line in lines:
        body, _, given = line[1:].partition("*")
        checksum = 0
        for char in body:
            checksum ^= ord(char)
        assert f"{checksum:02X}" == given, line


def test_the_flaky_example_really_breaks_things():
    kinds = {f.kind for d in load(ROOT / "examples" / "flaky-meter.toml").devices for f in d.faults}
    assert len(kinds) >= 3


def test_the_committed_transcript_is_what_the_simulator_prints_today():
    done = subprocess.run(
        ["bash", str(ROOT / "examples" / "demo" / "generate.sh"), "--check"],
        env={**os.environ, "PYTHON": sys.executable},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_pytest_example_passes():
    """examples/pytest is run as its own pytest session, the way a user's repository would."""
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT / "examples" / "pytest",
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert re.search(r"\b[1-9]\d* passed\b", done.stdout)


def test_the_example_workflow_names_files_the_example_scenario_provides():
    text = (ROOT / "examples" / "ci" / "serial-tests.yml").read_text(encoding="utf-8")
    assert "serial-sim exec" in text and "SERIAL_SIM_METER" in text
    assert (ROOT / "examples" / "flaky-meter.toml").exists()
