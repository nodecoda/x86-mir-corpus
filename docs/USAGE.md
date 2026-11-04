# Usage

All commands are available through the `x86-mir-corpus` console script or
`python3 -m x86_mir_corpus`.

## Paths and environment

| Variable | Meaning |
|---|---|
| `X86_MIR_LLVM_SRC` | llvm-project checkout (also the default `--repo`) |
| `X86_MIR_LLVM_BUILD` | LLVM build dir (contains `lib/cmake/llvm`) |
| `X86_MIR_SIDECAR_BUILD` | dir with `x86-mir-oracle` / `x86-mir-run` |
| `X86_MIR_ORACLE` | explicit sidecar oracle binary |
| `X86_MIR_RUN` | explicit sidecar MIR driver |

CLI flags win over environment variables.  `doctor` reports which paths were
resolved.

## Doctor

```bash
x86-mir-corpus doctor --json
x86-mir-corpus llvm doctor --json      # same checks
```

Checks: Python >= 3.10, LLVM revision pin, assertions enabled, X86 target,
sidecar binaries and build info, frozen reference provenance, schema versions.
Exit 0 when `ok: true`, exit 1 otherwise with actionable `reasons[]`.

## Sidecar build

```bash
x86-mir-corpus llvm build \
  --llvm-src "$X86_MIR_LLVM_SRC" \
  --llvm-build "$X86_MIR_LLVM_BUILD" \
  --out .build
```

Produces `.build/x86-mir-oracle` and `.build/x86-mir-run`.  See
[LLVM-INTEGRATION.md](LLVM-INTEGRATION.md) for the `-UNDEBUG` requirement.

## Parse and corpus

Read the committed snapshot:

```bash
x86-mir-corpus parse \
  --input src/x86_mir_corpus/assets/source-snapshots/fed20e0e-X86LegalizerInfo.cpp \
  --config C2 --out /tmp/ast.json

x86-mir-corpus points \
  --input src/x86_mir_corpus/assets/source-snapshots/fed20e0e-X86LegalizerInfo.cpp \
  --config C2 --tier witness --out /tmp/points.json
```

Read a revision from an LLVM checkout (default `--source-rev upstream/main`):

```bash
x86-mir-corpus corpus --repo "$X86_MIR_LLVM_SRC" \
  --source-rev upstream/main --config C2 --oracle=none \
  --tier witness --out /tmp/shapes.json
```

Fill expected actions with the frozen reference:

```bash
x86-mir-corpus corpus \
  --input src/x86_mir_corpus/assets/source-snapshots/fed20e0e-X86LegalizerInfo.cpp \
  --config C2 --tier witness \
  --oracle=reference --oracle-binary .build/x86-mir-oracle \
  --allow-cross-revision --out /tmp/up-C2.json
```

`--allow-cross-revision` is required when the input snapshot and the frozen
reference are different commits (today `fed20e0e` vs `ebac17721d12`); the
corpus records `oracle.cross_revision: true`.  The legacy path is
`--build "$X86_MIR_LLVM_BUILD"` (uses in-tree `X86Tests`).

Coverage gate: a rule that has no verified probe, boundary point, or reviewed
exception exits 3.  Reviewed exceptions live in
`src/x86_mir_corpus/assets/coverage_exceptions.json`.

## MIR run

```bash
x86-mir-corpus mir-run --corpus /tmp/up-C2.json \
  --driver .build/x86-mir-run --rules production \
  --shard-size 2000 --jobs 4 --timeout 30 --max-steps 10000 \
  --out /tmp/report.json
```

`--driver` can also be the legacy in-tree `X86Tests` binary; the supervisor
adds the gtest filter only for that binary.

Exit codes: 0 = report written and A3 met; 1 = report written, A3 not met
(crashes/budgets outside the known set); 2 = infrastructure error (no report
you can trust).

## Baseline

```bash
x86-mir-corpus baseline capture --report /tmp/report.json \
  --out /tmp/baseline.json
x86-mir-corpus baseline check --report /tmp/report.json \
  --baseline /tmp/baseline.json --json
```

`check` exits 0 when the atom projection and counts are unchanged, else 1.
`added`/`removed` list exact atom ids and fields.  The baseline is a known set,
not a zero-anomaly claim.

## Triage

```bash
x86-mir-corpus triage cluster \
  --report /tmp/report.json --corpus /tmp/full-C2.json \
  --driver .build/x86-mir-run --out /tmp/clusters.json \
  --markdown /tmp/clusters.md

x86-mir-corpus triage verify \
  --clusters /tmp/clusters.json --corpus /tmp/full-C2.json \
  --driver .build/x86-mir-run --out /tmp/verified.json
```

Crashes are grouped by assertion site; budgets by `(opcode, type shape)`.
Output has no timestamps and is sorted, so two runs are byte-identical.

## Trace (legacy, Phase 1)

`trace-diff` compares two trace JSON documents.  The trace drivers themselves
(`TraceCorpusWithReference`, `TraceCorpusWithTable`) remain in the in-tree
`X86Tests` binary:

```bash
scripts/run-traces.sh /tmp/up-C2.json /tmp/traces "$X86_MIR_LLVM_BUILD"
```
