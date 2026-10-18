"""The reference documentation, generated from the same tables the code uses."""

from __future__ import annotations

from .modbus import EXCEPTIONS, FUNCTIONS
from .scenario import FAULT_KINDS, SECTIONS


def reference_markdown() -> str:
    """Every key a scenario file may use, section by section."""
    out = []
    for name, keys in SECTIONS.items():
        out.append(f"#### {name}\n")
        out.append("| Key | Type | Default | Meaning |")
        out.append("| --- | --- | --- | --- |")
        for k in keys:
            default = "**required**" if k.required else (f"`{k.default}`" if k.default else "")
            out.append(f"| `{k.name}` | {k.type} | {default} | {k.doc} |")
        out.append("")
    return "\n".join(out).rstrip("\n")


def faults_markdown() -> str:
    rows = ["| Fault | Applies to | What the client sees | Its own keys |", "| --- | --- | --- | --- |"]
    for kind, (applies, sees, own) in FAULT_KINDS.items():
        keys = ", ".join(f"`{k}`" for k in own) or "none"
        rows.append(f"| `{kind}` | {'every kind' if applies == 'all' else applies} | {sees} | {keys} |")
    return "\n".join(rows)


def functions_markdown() -> str:
    rows = ["| Code | Function |", "| --- | --- |"]
    rows += [f"| {code} | {name} |" for code, name in FUNCTIONS.items()]
    rows += ["", "| Exception | Name | When |", "| --- | --- | --- |"]
    rows += [f"| {code} | {name} | {when} |" for code, (name, when) in EXCEPTIONS.items()]
    return "\n".join(rows)


def everything() -> str:
    return "\n\n".join(
        [
            "### Scenario file reference\n\n" + reference_markdown(),
            "### Faults\n\n" + faults_markdown(),
            "### Modbus functions\n\n" + functions_markdown(),
        ]
    )
