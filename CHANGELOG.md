# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/), versions follow [SemVer](https://semver.org/).

## Unreleased

### Added
- `bytewise = true` on a device (needs `baud`): a reply is sent one character at a time, each when it would have left
  the line, instead of as one piece. The transcript has one record per character. For software with a time limit
  between characters.
- `validate` says "bytewise" for such a device.
- `docs/design-notes.md`: why the tool is built the way it is, what was rejected, how it was checked, and the
  questions this design usually draws.
- `ROADMAP.md`: the next small steps, in order.

## 0.1.0 - 2026-10-18

First release: Modbus RTU slaves, scripted consoles, line streams and echo devices on pseudo-terminals; seeded fault
injection; a transcript of every byte; `run`, `exec`, `validate`, `show` and `reference`.
