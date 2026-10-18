# Design notes

Why serial-sim is built the way it is. The README says what it does; this says what was decided and what else was
considered.

## The problem in one paragraph

Code that talks to hardware over a serial line is rarely tested for what hardware does badly: a reply that comes late,
a reply cut in two, a checksum that is wrong now and then, a device that answers "busy". On the bench those things
happen when they like, and the device is in another room. The aim is to make them happen on request, in CI, with the
same result every time, and to leave a record of every byte so a failure can be looked at.

## Decisions

### Pseudo-terminals, so the code under test cannot tell

The simulator creates a pty and a symbolic link to it. The software under test opens that path exactly as it would open
`/dev/ttyUSB0`, with whatever library it uses (pyserial, libmodbus, `open()`). Nothing in the code under test has to
change, and nothing has to be injected. The alternatives were a mock of the serial library, which tests the mock, a
TCP bridge, which changes the code path, or a kernel module (`tty0tty`, `socat` pairs), which needs privileges or
extra tools in CI. A pty needs none of that and exists on every Linux.

The price is that a pty is not a UART. Line settings are accepted and ignored, and there are no framing errors or modem
lines. That is the first limit in the README.

### Devices are described in a file, and the file is strict

A scenario is TOML. Unknown keys are errors with a suggestion ("did you mean ...?"), because a typo that is silently
ignored would turn a fault into no fault and a test into a pass. The same table of keys drives the checks and the
README's reference tables, so documentation and behaviour cannot drift.

### Faults are seeded

Probabilities and flipped bits come from a random generator seeded per device. The same seed gives the same run, so a
failure found once can be reproduced, and another seed explores another path. A fault can also be chosen by request
number (`every`, `after`, `until`) or, for Modbus, by function code, for the case where "the third poll fails" is the
thing to test.

### A transcript of every byte

Every read and write is recorded with a time, as one JSON object per line, with hex and text. When a test fails, the
question is always "what actually crossed the wire?", and this answers it without a logic analyser. `show --no-time`
makes two runs comparable.

### Modbus is implemented from the specification, then checked against other people's software

The Modbus RTU slave follows the Application Protocol specification: its worked examples are tests. But tests written
by the same hand that wrote the frames would share its mistakes, so pymodbus, `mbpoll` (libmodbus) and pyserial are
also used as clients against the simulator. If the frames were wrong, they would fail where the author's tests would
not.

### One loop, one thread

All devices are served by one selector loop. It is simple and deterministic and enough for the intended use (a few
devices); the README says that many busy devices share the loop.

### Timing is approximate, and says so

Delays come from a Python loop on an ordinary Linux machine, so a reply is never early and is late by whatever the
scheduler adds. With `baud` set a reply takes `length * char_bits / baud` seconds. By default it arrives as one piece
when its last character would have left the line; with `bytewise = true` each character arrives when it would have, which
matters for software with a time limit between characters. It is for slow and late devices, not for measuring turnaround.

## How it was checked

* Against the specification (the worked examples, every exception, the limits), and against pymodbus, `mbpoll` and
  pyserial as clients.
* On real pseudo-terminals: timing, faults, greetings waiting for a first reader, close and reopen, two devices, a
  client that never reads, and the lifecycle of the links.
* Noise: random bytes into the Modbus parser and the script devices, and 150 mutated scenario files that may only
  produce a scenario or a `ScenarioError`.
* The documentation is checked against the code, and the transcript in the README is compared with a fresh run in CI.
* **A mutation check, once and by hand.** A script applied 184 small mutations to a copy of the sources and ran the
  whole suite against each. 22 survived the first pass; each got a test and all 184 are now caught. The script is not in
  the repository and not in CI.

## What would be changed next

See [ROADMAP.md](../ROADMAP.md). In order: Modbus ASCII framing, a random extra delay (jitter), following a transcript
while it grows, several slaves on one port, and a Modbus master.

## Questions this design usually draws

**Why a pty and not a mock?** The code under test opens a real path and cannot tell the difference. A mock tests the
mock.

**Is it a real-time simulator?** No. Timing is a Python loop on a normal machine: never early, sometimes late. It tests
behaviour against slow or late devices, not exact turnaround.

**How do you make a failure reproducible?** A seed. Same seed, same faults, and a transcript to compare.

**How do you know the Modbus frames are right?** The specification's examples are tests, and three independent clients
(pymodbus, mbpoll, pyserial) talk to the simulator in CI.

**What does it not do?** Anything below the byte stream: parity and framing errors, modem lines, RS-485 direction
control. One device is one address; there is no multi-drop bus.

## Three-minute demo

```sh
pip install git+https://github.com/ErenOzgu/serial-sim
git clone https://github.com/ErenOzgu/serial-sim && cd serial-sim
sudo apt-get install mbpoll
serial-sim exec examples/demo/meter.toml --log wire.jsonl -- \
    sh -c 'mbpoll -m rtu -a 17 -b 9600 -P none -t 4 -r 1 -c 3 -1 "$SERIAL_SIM_METER"'
serial-sim show wire.jsonl
```

What to say: `mbpoll` is a real Modbus master and has no idea it is talking to a simulator. It reads the three registers,
and the transcript shows the request and the reply byte by byte. Then run it again with `examples/flaky-meter.toml` to
see a master cope with dropped and corrupted replies, with the same seed giving the same faults.
