# Acceptance evidence (v0.1.0)

Environment: llvm-project `8bc4cd96f4bb`, LLVM build
`/home/ubuntu/llvm/builds/explore` (assertions ON, X86), corpus cache under
`.omx/evidence/x86-mir-corpus/corpus`, sidecar in `.build`.

## A. Packaging

```text
python3 -m venv /tmp/xmc-clean
/tmp/xmc-clean/bin/pip install .
/tmp/xmc-clean/bin/x86-mir-corpus --help        # exit 0
python -c "import x86_mir_corpus"               # 0.1.0
pip show x86-mir-corpus                         # Requires: (empty)
```

Package data includes the baseline, coverage exceptions, known deltas, the
`fed20e0e` source snapshot, provenance, and supported revisions.

## B. Unit tests

```text
Ran 133 tests in ~30 s
OK (skipped=4)
```

Skips are environment-gated: `upstream/main` in the local checkout, migration
needs an LLVM source, and three sidecar parity tests without
`X86_MIR_CORPUS_DIR`/`X86_MIR_LLVM_BUILD`.

Parity suite run with the cached corpora:

```text
Ran 3 tests in ~31 s
OK
```

## C. Doctor

```text
ok True
reasons []
src 8bc4cd96f4bb
assertions True
binaries ['x86-mir-oracle', 'x86-mir-run', 'x86-mir-smoke']
```

## D. Sidecar build and smoke

```text
x86-mir-corpus llvm build --llvm-src ... --llvm-build ... --out ...
# writes x86-mir-oracle and x86-mir-run, exit 0

mir-run --only-atom cpp/X86_BUILD_VECTOR/G_BUILD_VECTOR/rule/3
# outcome=budget steps=10001, exit 1 (A3 not met; expected A1 evidence)
```

## E. Oracle parity

Same points file (`up-C2.json.points.json`, 2217 atoms), in-tree `X86Tests`
filler vs `x86-mir-oracle`:

```text
sha256 7b7634f5ed50516e194d80523702b8f09b5973a454a54cc6aa38b26b80e0a769
# byte-identical
```

## F. MIR parity

C2, same supervisor, in-tree `X86Tests` vs `x86-mir-run`:

```text
counts equal: true
atom field diffs (outcome/steps/detail_code/failed_on_opcode): 0
full atom dicts excluding stderr_tail: 0 diffs
```

Four-config counts and crash atom ids equal
`src/x86_mir_corpus/assets/mir-baseline-witness.json`:

| config | atoms | budget | crash | failed | legalized | construction_mismatch | unbuildable |
|---|---:|---:|---:|---:|---:|---:|---:|
| C0 | 2062 | 5 | 24 | 1119 | 170 | 303 | 441 |
| C1 | 2169 | 21 | 24 | 1059 | 316 | 304 | 445 |
| C2 | 2217 | 20 | 24 | 1034 | 388 | 304 | 447 |
| C3 | 2093 | 5 | 24 | 1103 | 216 | 303 | 442 |

## G. Baseline

```text
baseline check (unchanged)                 exit 0, added=[], removed=[]
mutate one legalized atom -> failed        exit 1, one added + one removed
```

## H. Triage

Full C2 (208851 atoms):

```text
crash_clusters 12
budget_clusters 192
triage verify: checks 34, all_match true
```

## I. Determinism and conservation

```text
two mir-run passes: per-atom core fields identical, counts identical
atoms 2217, sum(counts) 2217, not_run 0
empty unbuildable reasons 0
```

## J. No runtime hardcoding

```bash
rg '/home/ubuntu|builds/explore' src/ sidecar/ scripts/   # no matches
```

## K. A2 sweep

Optional and not run in this pass.  `scripts/mir-a2-probe.sh` is parameterized
for `--driver`/`--build`; the previous A2 evidence remains the reference.
