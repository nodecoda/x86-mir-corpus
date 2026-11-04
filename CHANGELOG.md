# Changelog

All notable changes to this project are documented here.  The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project uses
`MAJOR.MINOR.PATCH` with `0.x` signalling an unstable report schema.

## [0.2.0] - 2026-11-04

### Added

- `dev` extras with `pytest`, `ruff`, and `build`; `[tool.ruff]` and
  `[tool.pytest.ini_options]` configuration.
- CI jobs for lint, wheel packaging plus CLI smoke, and the existing Python
  test matrix.
- `CONTRIBUTING.md`, `docs/README.md`, and `docs/PRODUCTIZATION.md`.
- `X86_MIR_PARITY=1` opt-in gate for the pinned-baseline parity test.
- `pin show` / `pin verify` / `pin update`: the bundled
  `X86LegalizerInfo.cpp` snapshot is pinned to LLVM commit
  `fed20e0e6832` in `assets/llvm-pin.json` (revision, sha256, frozen
  reference), and `pin verify --repo` re-extracts the blob from a checkout
  to prove the snapshot still matches.
- `x86-mir-corpus --version`.

### Changed

- `python -m unittest discover -s tests` is green on a fresh checkout: the
  pinned-baseline parity test no longer fails when the local LLVM revision has
  drifted from the recorded witness.
- Repository hygiene: `.gitignore` covers pytest/ruff/mypy caches and logs.

### Fixed

- `raise ... from` / `capture_output` / ambiguous names / redundant open modes
  across the package and tests (ruff `E4,E7,E9,F,I,B,UP,SIM,RUF` clean).

## [0.1.0] - 2026-11-04

### Added

- Parser for the hand-written `X86LegalizerInfo.cpp` rule chains.
- Witness, full, and deep corpus emission with schema-stable output.
- Frozen `X86LegalizerInfoReference` oracle and the out-of-tree sidecar
  drivers (`x86-mir-oracle`, `x86-mir-run`, `x86-mir-smoke`).
- `mir-run` outcomes (`legalized`, `failed`, `budget`, `crash`, `hang`,
  `construction_mismatch`, `unbuildable`, `not_run`), triage, baseline diff,
  and `doctor`.
