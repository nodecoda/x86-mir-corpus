# Changelog

All notable changes to this project are documented here.  The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project uses
`MAJOR.MINOR.PATCH` with `0.x` signalling an unstable report schema.

## [Unreleased]

### Added

- AArch64 target profile: quotient-derived boundary panel (fixed and scalable
  vectors, `iN`/`sN`, `bf16`/`f128`), A0–A3 feature configs, and the same
  schema-stable corpus/report surface as X86.
- Rule-witness probing for opaque builders (predicate-driven shapes), lifting
  AArch64 rule coverage to 582/611 active rules verified with 0 unprobed; the
  37 remaining rows carry reviewed `pipeline_unreachable` exceptions.
- `TargetProfile.type_fills` / `shape_arity_unreachable`: reviewed fills for
  opcode type indices the rules do not constrain (carry flags, shift counts,
  lane masks, reduction accumulators, build-vector elements, comparison
  operands, `G_IS_FPCLASS`); `shape_arity 16 -> 0`.
- `assets/aarch64-baseline-witness.json`: the AArch64 A3 family list (empty
  after the first review) and the target-aware `mirrun.is_known_family` seam.
- `corpora/aarch64/witness-A2.{points,report}.json` and the MANIFEST entry:
  13,854 atoms, A3 met; `corpora/aarch64/A2-full.report.json` records the
  1,677,533-atom full run (same 582/611 rules, crash/budget/hang zero).
- IR-shape type contracts for `G_CONCAT_VECTORS`, `G_INSERT_VECTOR_ELT`,
  `G_EXTRACT_VECTOR_ELT`, `G_BITCAST`, `G_SHUFFLE_VECTOR`, `G_FPTRUNC`,
  `G_VECREDUCE_SEQ_*`, `G_CTLS`, and same-type shifts, plus target-scoped
  scalable-constructibility exclusions.  They are enforced only for corpus
  target `aarch64`; an x86 C2 witness re-run (2,217 atoms) matches the HEAD
  build atom-for-atom, and four x86 fixture controls pin the scoping.
- `doctor --target aarch64`; the sidecar gains a `mir-run` build target
  (the `x86-mir-run` binary name stays for compatibility).
- `docs/AARCH64.md`, the AArch64 crash/arity analysis, and README/CI coverage.

### Changed

- `mir-run` reports carry `target` at the top level (also under
  `corpus.target`), so an AArch64 report is self-describing.
- Quotient reports list every class the boundary panel misses:
  `unrepresented_detail` gives an example and `panel_gap`/`opaque_predicate`
  per class, refreshed in `docs/quotient-{x86-C2,aarch64-A2}.json`.
- `llvm build` builds the `mir-run` target instead of the historical
  `x86-mir-run` target name (same binary).
- The AArch64 witness panel and rule probes changed the committed witness
  point set; X86 full-C2 points are unchanged and remain the frozen gate.

### Fixed

- Sidecar parity is now contract-aware: `x86-mir-run` must match `X86Tests`
  field-for-field on every atom it hands to the legalizer, while atoms it
  rejects in construction are exempt; no in-tree `crash`/`budget`/`hang` may
  become `legalized`/`failed` evidence.  LLVM `6706952f4a5a` joins the
  supported revisions after the gate passed.
- AArch64 corpus construction crashes: `G_ROTL` float funnel shifts,
  scalable `G_IS_FPCLASS` (`buildConstant` assertion), and scalable
  zero-poison/compare/multiply shapes are now `construction_mismatch`
  before the legalizer runs.
- `TypeSize::getFixedValue()` is guarded for scalable vectors in the driver's
  contract and width checks.

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
- `x86-mir-run` checks each atom's type slots against per-opcode contracts
  before building it.  Atoms that no IR producer can create (pointer carry,
  non-i1 `icmp` result, pointer shift operand, vector/scalar mismatch) are now
  `construction_mismatch` with `detail_code: type_contract` instead of
  aborting the driver and forcing the shard to restart.  The check follows IR
  type rules, so it also reclassifies atoms the legalizer happened to tolerate
  (e.g. an `fcmp` with an i256 result); on the 208,851-atom C2 corpus this
  turns 3,265 crashes into 0 and drops the full run from hours to ~10 minutes
  at 8 jobs.

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
