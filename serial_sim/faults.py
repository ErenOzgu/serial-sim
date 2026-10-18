"""Fault injection: which requests are hit, and what a hit does to the reply.

Choosing is deterministic: `every`, `after`, `until` and `functions` look only at the request number and the
function code, and `probability` draws from one seeded generator per device, only for requests that passed the
other tests. The same scenario and the same requests therefore always break the same way.
"""

from __future__ import annotations

import random

from .exchange import Emission, Exchange
from .scenario import MODBUS_FAULTS, FaultSpec


def selected(
    faults: tuple[FaultSpec, ...], number: int, function: int | None, rng: random.Random
) -> list[FaultSpec]:
    """The faults that hit request `number` (from 1): protocol faults first, then in file order."""
    hit = []
    for spec in faults:
        if number <= spec.after:
            continue
        if spec.until is not None and number > spec.until:
            continue
        if spec.every is not None and number % spec.every != 0:
            continue
        if spec.functions and function not in spec.functions:
            continue
        if spec.probability is not None and rng.random() >= spec.probability:
            continue
        hit.append(spec)
    return sorted(hit, key=lambda s: s.kind not in MODBUS_FAULTS)


def _flip(emissions: list[Emission], bits: int, rng: random.Random) -> list[Emission]:
    total = sum(len(e.data) for e in emissions) * 8
    if not total:
        return emissions
    positions = rng.sample(range(total), min(bits, total))
    data = [bytearray(e.data) for e in emissions]
    for pos in positions:
        byte, bit = divmod(pos, 8)
        for chunk in data:
            if byte < len(chunk):
                chunk[byte] ^= 1 << bit
                break
            byte -= len(chunk)
    return [Emission(e.delay, bytes(d)) for e, d in zip(emissions, data, strict=True)]


def _truncate(emissions: list[Emission], keep: int) -> list[Emission]:
    out = []
    for e in emissions:
        if keep <= 0:
            break
        out.append(Emission(e.delay, e.data[:keep]))
        keep -= len(e.data)
    return out


def apply_generic(spec: FaultSpec, emissions: list[Emission], rng: random.Random) -> list[Emission]:
    """One of the byte-level faults, applied to the parts of a reply."""
    if not emissions:
        return emissions
    kind = spec.kind
    if kind == "drop":
        return []
    if kind == "delay":
        return [Emission(emissions[0].delay + spec.ms / 1000, emissions[0].data), *emissions[1:]]
    if kind == "corrupt":
        return _flip(emissions, spec.bits, rng)
    if kind == "garbage":
        noise = bytes(rng.randrange(256) for _ in range(spec.count))
        return [Emission(emissions[0].delay, noise + emissions[0].data), *emissions[1:]]
    if kind == "truncate":
        total = sum(len(e.data) for e in emissions)
        return _truncate(emissions, spec.keep if spec.keep is not None else total // 2)
    if kind == "split":
        first = emissions[0]
        at = spec.at if spec.at is not None else len(first.data) // 2
        if not 0 < at < len(first.data):
            return emissions
        return [
            Emission(first.delay, first.data[:at]),
            Emission(spec.ms / 1000, first.data[at:]),
            *emissions[1:],
        ]
    if kind == "duplicate":
        return [*emissions, *(Emission(0.0 if i == 0 else e.delay, e.data) for i, e in enumerate(emissions))]
    raise ValueError(f"not a byte-level fault: {kind}")


def apply(exchange: Exchange, specs: list[FaultSpec], behaviour, rng: random.Random) -> Exchange:
    """Apply the chosen faults to an exchange, in order; a dropped reply ends the chain."""
    for spec in specs:
        if not exchange.emissions:
            break
        if spec.kind in MODBUS_FAULTS:
            exchange = behaviour.protocol_fault(spec, exchange)
        else:
            exchange.emissions = apply_generic(spec, exchange.emissions, rng)
        exchange.notes.append(spec.kind)
    return exchange
