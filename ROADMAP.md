# Roadmap

Small steps, in order. A step is done when it has tests, the README says what changed, CI is green and the CHANGELOG
has a line for it. Each step is meant to be a few hours of work, not a rewrite.

- [x] `bytewise = true`: characters one at a time at the baud rate
- [ ] Modbus ASCII framing for `modbus-rtu` devices as a second `kind`
- [x] A `jitter_ms` option: a random extra delay per reply, from the seeded generator
- [ ] `show --follow`: print a transcript as it grows, for watching a run
- [ ] Several slaves on one link (a bus), starting with two units sharing a port
- [ ] A Modbus master `kind`, so that two simulators can talk to each other in a test of the master code path
