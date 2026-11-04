# x86-mir-corpus

Tooling for the X86 and AArch64 GlobalISel legalizer corpus: read the
hand-written `X86LegalizerInfo.cpp` / `AArch64LegalizerInfo.cpp`, emit a
legality-point corpus, and drive real `MachineFunction` legalization through
an external LLVM build.

**This is not part of LLVM.**  It does not patch or fork llvm-project.  The
Python package needs no third-party dependencies; the C++ sidecar links a
pinned LLVM build.

## What it does

- **Parse** the C++ rule chains (including the feature guards) into a
  deterministic AST and rule census.
- **Emit** witness/full/deep legality points and a schema-stable corpus.
- **Fill expected actions** from the frozen `X86LegalizerInfoReference`
  (`x86-mir-oracle`) or from the legacy in-tree `X86Tests`.
- **Run** every corpus atom through the real legalizer (`x86-mir-run`, shared
  by both targets) and
  report one explicit outcome per atom: `legalized`, `failed`, `budget`,
  `crash`, `hang`, `construction_mismatch`, `unbuildable`, or `not_run`.
- **Triage** crashes by assertion site and budgets by operand shape.
- **Check** a report against a known baseline and report only added/removed
  atoms.

The evidence semantics are deliberately conservative: `not_run` is never
silently folded into a success, infrastructure errors exit 2, and a run with
new crashes/budgets exits 1.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
x86-mir-corpus --help
x86-mir-corpus pin show        # which LLVM commit the snapshot is pinned to
```

Bundled witness corpora and reports live in `corpora/` (see
`corpora/README.md`).  Requires Python >= 3.10.  See the
[docs index](docs/README.md), [docs/USAGE.md](docs/USAGE.md) for commands, and
[docs/LLVM-INTEGRATION.md](docs/LLVM-INTEGRATION.md) for the sidecar build.

## Quality gates

```bash
pip install -e ".[dev]"
ruff check src tests
python -m unittest discover -s tests -q
```

The pinned-baseline parity test only runs with `X86_MIR_PARITY=1` against the
LLVM revision its witness was recorded on; see
[docs/PRODUCTIZATION.md](docs/PRODUCTIZATION.md) for the refresh procedure.

## DSL surface

The parser vocabulary is one generated table
(`src/x86_mir_corpus/assets/dsl_symbols.json`), not a list of names in code.
`dsl audit` classifies the whole target source by region and fails on an
unregistered constructor/lambda symbol:

```bash
x86-mir-corpus dsl verify --repo "$X86_MIR_LLVM_SRC" \
  --revision "$(x86-mir-corpus pin show --json | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["revision"])')"
x86-mir-corpus dsl audit --target aarch64 --repo "$X86_MIR_LLVM_SRC" --strict
x86-mir-corpus dsl audit --target x86 \
  --input src/x86_mir_corpus/assets/source-snapshots/fed20e0e-X86LegalizerInfo.cpp \
  --strict
```

See [docs/DSL-SURFACE.md](docs/DSL-SURFACE.md) for the section map, the
three-way consistency rule, the region model, and the pin-bump flow.

## Quick start

```bash
export X86_MIR_LLVM_SRC=/path/to/llvm-project
export X86_MIR_LLVM_BUILD=/path/to/llvm-build
export X86_MIR_SIDECAR_BUILD=$PWD/.build

x86-mir-corpus llvm build \
  --llvm-src "$X86_MIR_LLVM_SRC" \
  --llvm-build "$X86_MIR_LLVM_BUILD" --out "$X86_MIR_SIDECAR_BUILD"

x86-mir-corpus doctor --json

x86-mir-corpus corpus \
  --input src/x86_mir_corpus/assets/source-snapshots/fed20e0e-X86LegalizerInfo.cpp \
  --config C2 --oracle=reference --oracle-binary .build/x86-mir-oracle \
  --out /tmp/up-C2.json

x86-mir-corpus mir-run --corpus /tmp/up-C2.json \
  --driver .build/x86-mir-run --out /tmp/report.json
```

## AArch64

The AArch64 profile parses the pinned `AArch64LegalizerInfo.cpp`, uses a
quotient-reviewed boundary panel (fixed and scalable vectors, the `iN`/`sN`
split, `bf16`/`f128`), and drives the same production legalizer:

```bash
x86-mir-corpus corpus --target aarch64 --config A2 --tier witness \
  --repo "$X86_MIR_LLVM_SRC" --allow-coverage-gaps --oracle=none \
  --out /tmp/a64-witness.json
x86-mir-corpus mir-run --corpus /tmp/a64-witness.json \
  --driver .build/x86-mir-run --rules production \
  --jobs 8 --shard-size 300 --timeout 30 --max-steps 10000 \
  --out /tmp/a64-witness.report.json
x86-mir-corpus doctor --target aarch64 --json

# finite-quotient diagnostic (read-only; see docs/QUOTIENT.md)
PYTHONPATH=src python3 scripts/quotient_report.py --target aarch64 --config A2 \
  --repo "$X86_MIR_LLVM_SRC" --out /tmp/quotient-a64-A2.json
```

The committed A2 witness is `corpora/aarch64/witness-A2.points.json`
(13,854 atoms, A3 met).  See [docs/AARCH64.md](docs/AARCH64.md) for the
evidence levels, coverage, type-arity fills, known gaps, and the full-run
baseline.  AArch64 has no frozen reference oracle; it is a production-rule
termination/constructibility corpus.

## Provenance

- Pinned source snapshot: llvm-project `fed20e0e6832`, sha256 `79f23760...`
  (`src/x86_mir_corpus/assets/llvm-pin.json`; check with
  `x86-mir-corpus pin verify`).  The pin is a fixed commit, not a branch.
- AArch64 pin: the same commit, snapshot
  `fed20e0e-AArch64LegalizerInfo.cpp`
  (`src/x86_mir_corpus/assets/aarch64-pin.json`).
- Extract commit for the upstream corpus utilities: `8bc4cd96f4bb`
  (`SOURCE-COMMIT`).
- Default C++ input: `fed20e0e-X86LegalizerInfo.cpp` (bundled with the wheel).
- Frozen reference: `ebac17721d12`, checked by `doctor` against the LLVM
  checkout and recorded in `sidecar/reference/`.
- License: Apache-2.0 WITH LLVM-exception (`LICENSE`, `NOTICE`).

## Status

Phase 1: Python tool, sidecar oracle/MIR drivers, parity gates, triage,
baseline, and doctor are implemented.  The two trace drivers remain legacy
(`mir-run --driver $LLVM_BUILD/unittests/Target/X86/X86Tests` keeps the old
gtest entry points working).

## Repository layout

```
src/x86_mir_corpus/   Python package (parser, corpus emission, runner, triage)
sidecar/              out-of-tree LLVM drivers in C++ (oracle, MIR runner, smoke)
corpora/              witness corpora; generated full corpora live in corpora/full/ (ignored)
docs/                 usage, pipeline, evidence contract, governance (see docs/README.md)
tests/                unittest suite, also runnable with pytest
scripts/              nightly and upstream drift automation
```
