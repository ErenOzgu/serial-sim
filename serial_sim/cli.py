"""The command line: run the devices, run a command against them, check a scenario, read a transcript."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .docs import everything, faults_markdown, functions_markdown, reference_markdown
from .scenario import Scenario, ScenarioError, load
from .transcript import format_record
from .transcript import read as read_transcript

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ENV = 3
EXIT_SIMULATOR = 4
EXIT_COMMAND_NOT_FOUND = 127


def _positive(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return value


def _link(text: str) -> tuple[str, str]:
    name, sep, path = text.partition("=")
    if not sep or not name or not path:
        raise argparse.ArgumentTypeError(f"{text!r} is not NAME=PATH")
    return name, path


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("scenario", help="the scenario file (TOML)")
    p.add_argument(
        "--link",
        action="append",
        default=[],
        type=_link,
        metavar="NAME=PATH",
        help="use PATH as the link of device NAME instead of the one in the scenario (repeatable)",
    )
    p.add_argument(
        "--dir",
        metavar="DIR",
        help="put the links of devices without a `link` in DIR (default: a new temporary directory)",
    )
    p.add_argument(
        "--log", metavar="FILE", help="write a transcript of everything on the wire, one JSON object per line"
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="serial-sim",
        description="Simulated serial devices on pseudo-terminals, with fault injection.",
    )
    p.add_argument("--version", action="version", version=f"serial-sim {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="start the devices and keep them running until stopped")
    _common(run)
    run.add_argument("--duration", type=_positive, metavar="SECONDS", help="stop by itself after this long")
    run.add_argument(
        "--ready-file",
        metavar="FILE",
        help="write a JSON file with the link of every device once they exist (removed at exit)",
    )
    run.add_argument("-q", "--quiet", action="store_true", help="do not print the table of devices")

    ex = sub.add_parser(
        "exec", help="start the devices, run a command, stop the devices; exit with its status"
    )
    _common(ex)

    val = sub.add_parser("validate", help="check a scenario file and say what it describes")
    val.add_argument("scenario")

    ref = sub.add_parser("reference", help="print the scenario file reference as Markdown")
    ref.add_argument(
        "part", nargs="?", choices=["scenario", "faults", "functions"], help="only this part (default: all)"
    )

    show = sub.add_parser("show", help="print a transcript for people")
    show.add_argument("log")
    show.add_argument("--device", metavar="NAME", help="only this device")
    show.add_argument("--no-time", action="store_true", help="leave out the timestamps (to compare runs)")
    show.add_argument("--text", action="store_true", help="also show the bytes as text")
    return p


def _fail(message: str, code: int) -> int:
    print(f"serial-sim: {message}", file=sys.stderr)
    return code


def _describe(sc: Scenario) -> list[str]:
    out = []
    for d in sc.devices:
        bits = [d.kind]
        if d.kind == "modbus-rtu":
            bits.append(f"unit {d.unit}, {len(d.blocks)} block(s)")
        elif d.kind == "script":
            bits.append(f"{len(d.rules)} rule(s)")
        elif d.kind == "stream":
            bits.append(f"{len(d.lines)} line(s) every {d.interval * 1000:g} ms")
        if d.baud:
            bits.append(f"{d.baud} baud" + (", bytewise" if d.bytewise else ""))
        if d.faults:
            bits.append("faults: " + ", ".join(f.kind for f in d.faults))
        out.append(f"{d.name:<12} " + "; ".join(bits))
    return out


def _cmd_validate(args: argparse.Namespace) -> int:
    sc = load(args.scenario)
    print(f"{args.scenario}: {len(sc.devices)} device(s), seed {sc.seed}")
    for line in _describe(sc):
        print("  " + line)
    return EXIT_OK


def _cmd_reference(args: argparse.Namespace) -> int:
    parts = {"scenario": reference_markdown, "faults": faults_markdown, "functions": functions_markdown}
    print(parts[args.part]() if args.part else everything())
    return EXIT_OK


def _cmd_show(args: argparse.Namespace) -> int:
    try:
        for record in read_transcript(args.log):
            if args.device and record["device"] != args.device:
                continue
            print(format_record(record, show_time=not args.no_time, show_text=args.text))
    except (OSError, ValueError) as exc:
        return _fail(str(exc), EXIT_USAGE)
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return EXIT_OK


def _engine(args: argparse.Namespace, sc: Scenario):
    try:
        from .engine import Engine
        from .link import LinkError
        from .transcript import Transcript
    except ImportError as exc:  # no termios / pty on this system
        raise _EnvError(f"this system has no pseudo-terminals to use ({exc})") from exc
    try:
        transcript = Transcript(args.log)
    except OSError as exc:
        raise _EnvError(f"{args.log}: {exc.strerror or exc}") from exc
    try:
        return Engine(sc, link_dir=args.dir, links=dict(args.link), transcript=transcript)
    except LinkError as exc:
        transcript.close()
        raise _UsageError(str(exc)) from exc


class _EnvError(Exception):
    pass


class _UsageError(Exception):
    pass


def _open(engine) -> dict[str, str]:
    from .link import LinkError

    try:
        return engine.open()
    except LinkError as exc:
        raise _EnvError(str(exc)) from exc


def _write_ready(path: str, paths: dict[str, str]) -> None:
    target = Path(path)
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps({"pid": os.getpid(), "devices": paths}, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, target)


def _cmd_run(args: argparse.Namespace) -> int:
    sc = load(args.scenario)
    engine = _engine(args, sc)
    stop = threading.Event()
    previous = {s: signal.signal(s, lambda *_: stop.set()) for s in (signal.SIGINT, signal.SIGTERM)}
    ready = args.ready_file
    try:
        paths = _open(engine)
        if not args.quiet:
            for d in sc.devices:
                print(f"{d.name:<12} {d.kind:<11} {paths[d.name]}", flush=True)
        if ready:
            try:
                _write_ready(ready, paths)
            except OSError as exc:
                raise _EnvError(f"{ready}: {exc.strerror or exc}") from exc
        limit = time.monotonic() + args.duration if args.duration else None
        engine.serve(lambda: stop.is_set() or (limit is not None and time.monotonic() >= limit))
    finally:
        engine.close()
        if ready:
            Path(ready).unlink(missing_ok=True)
        for s, h in previous.items():
            signal.signal(s, h)
    return EXIT_OK


def _cmd_exec(args: argparse.Namespace) -> int:
    argv = args.command_argv
    if not argv:
        return _fail("exec needs a command after `--`", EXIT_USAGE)
    sc = load(args.scenario)
    engine = _engine(args, sc)
    failure: list[BaseException] = []
    stop = threading.Event()

    def serve() -> None:
        try:
            engine.serve(stop.is_set)
        except BaseException as exc:  # reported after the command ends
            failure.append(exc)

    try:
        paths = _open(engine)
        env = dict(os.environ)
        for d in sc.devices:
            env[d.env_name] = paths[d.name]
        env["SERIAL_SIM_DEVICES"] = json.dumps(paths)
        thread = threading.Thread(target=serve, name="serial-sim", daemon=True)
        thread.start()
        try:
            proc = subprocess.Popen(argv, env=env)
        except OSError as exc:
            stop.set()
            thread.join(2)
            return _fail(f"cannot run {argv[0]}: {exc.strerror or exc}", EXIT_COMMAND_NOT_FOUND)
        previous = signal.signal(signal.SIGTERM, lambda *_: proc.send_signal(signal.SIGTERM))
        try:
            while True:
                try:
                    code = proc.wait()
                    break
                except KeyboardInterrupt:
                    proc.send_signal(signal.SIGINT)
        finally:
            signal.signal(signal.SIGTERM, previous)
            stop.set()
            thread.join(2)
    finally:
        engine.close()
    if failure:
        return _fail(
            f"the simulator stopped with an error while the command ran: {failure[0]!r}", EXIT_SIMULATOR
        )
    return code if code >= 0 else 128 - code


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command: list[str] = []
    if argv[:1] == ["exec"] and "--" in argv:
        split = argv.index("--")
        argv, command = argv[:split], argv[split + 1 :]
    parser = build_parser()
    args = parser.parse_args(argv)
    args.command_argv = command
    try:
        if args.command == "validate":
            return _cmd_validate(args)
        if args.command == "show":
            return _cmd_show(args)
        if args.command == "reference":
            return _cmd_reference(args)
        if args.command == "run":
            return _cmd_run(args)
        return _cmd_exec(args)
    except ScenarioError as exc:
        return _fail(str(exc), EXIT_USAGE)
    except _UsageError as exc:
        return _fail(str(exc), EXIT_USAGE)
    except _EnvError as exc:
        return _fail(str(exc), EXIT_ENV)
    except BrokenPipeError:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
