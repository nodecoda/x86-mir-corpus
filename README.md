# x86-mir-corpus

Tooling for the X86 GlobalISel legalizer corpus: read the hand-written
`X86LegalizerInfo.cpp`, emit a legality-point corpus, and drive real
`MachineFunction` legalization through an external LLVM build.

**This is not part of LLVM.**  It does not patch or fork llvm-project.  The
Python package needs no third-party dependencies; the C++ sidecar links a
pinned LLVM build.

## What it does

- **Parse** the C++ rule chains (including the feature guards) into a
  deterministic AST and rule census.
- **Emit** witness/full/deep legality points and a schema-stable corpus.
- **Fill expected actions** from the frozen `X86LegalizerInfoReference`
  (`x86-mir-oracle`) or from the legacy in-tree `X86Tests`.
- **Run** every corpus atom through the real legalizer (`x86-mir-run`) and
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
```

Requires Python >= 3.10.  See [docs/USAGE.md](docs/USAGE.md) for commands and
[docs/LLVM-INTEGRATION.md](docs/LLVM-INTEGRATION.md) for the sidecar build.

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

## Provenance

- Source snapshot: llvm-project `8bc4cd96f4bb` (`SOURCE-COMMIT`).
- Default C++ input: `fed20e0e-X86LegalizerInfo.cpp`.
- Frozen reference: `ebac17721d12`, checked by `doctor` against the LLVM
  checkout and recorded in `sidecar/reference/`.
- License: Apache-2.0 WITH LLVM-exception (`LICENSE`, `NOTICE`).

## Status

Phase 1: Python tool, sidecar oracle/MIR drivers, parity gates, triage,
baseline, and doctor are implemented.  The two trace drivers remain legacy
(`mir-run --driver $LLVM_BUILD/unittests/Target/X86/X86Tests` keeps the old
gtest entry points working).
