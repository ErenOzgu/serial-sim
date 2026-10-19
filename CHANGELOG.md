# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/), versions follow [SemVer](https://semver.org/).

## Unreleased

### Added
- `jitter_ms` on a device: each reply waits a random extra time of 0 up to that many milliseconds, drawn from its own
  seeded generator (same seed, same delays; the choice of faults is unchanged). Echo, greetings and prompts are not
  delayed. `validate` shows it.
- `bytewise = true` on a device (needs `baud`): a reply is sent one character at a time, each when it would have left
  the line, instead of as one piece. The transcript has one record per character. For software with a time limit
  between characters.
- `validate` says "bytewise" for such a device.
- `docs/design-notes.md`: why the tool is built the way it is, what was rejected, how it was checked, and the
  questions this design usually draws.
- `ROADMAP.md`: the next small steps, in order.

### Fixed
- A client that never reads: the pending output stayed under its 64 KiB bound only after each pass of the loop; for a
  moment inside the pass it could be one reply over. It is now bounded at every moment (the newest bytes are still the
  ones dropped, and the event is written once per pass). A test that watched from another thread caught it on one CI run.

### Changed
- Releases: a commit that adds a numbered heading to this file releases that version (the Release workflow tests, builds and
  makes the tag itself). Pushing a `vX.Y.Z` tag or running the workflow by hand still works.

## 0.1.0 - 2026-10-18

First release: Modbus RTU slaves, scripted consoles, line streams and echo devices on pseudo-terminals; seeded fault
injection; a transcript of every byte; `run`, `exec`, `validate`, `show` and `reference`.
