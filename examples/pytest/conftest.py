"""A pytest fixture that runs serial-sim for the length of a test session and hands out the port path."""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCENARIO = Path(__file__).resolve().parent.parent / "demo" / "meter.toml"


@pytest.fixture(scope="session")
def meter_port(tmp_path_factory):
    ready = tmp_path_factory.mktemp("serial-sim") / "ready.json"
    proc = subprocess.Popen(
        [sys.executable, "-m", "serial_sim", "run", str(SCENARIO), "--ready-file", str(ready), "-q"]
    )
    try:
        for _ in range(200):  # the file appears once every device exists
            if ready.exists():
                break
            if proc.poll() is not None:
                raise RuntimeError(f"serial-sim exited with status {proc.returncode}")
            time.sleep(0.05)
        else:
            raise RuntimeError("serial-sim did not start within 10 seconds")
        yield json.loads(ready.read_text())["devices"]["meter"]
    finally:
        proc.terminate()
        proc.wait(timeout=10)
