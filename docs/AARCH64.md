# AArch64 legalizer corpus

The AArch64 profile applies the same corpus schema, supervisor, and evidence
contract as X86 to `AArch64LegalizerInfo.cpp`.  It is a **production-rule
termination/constructibility corpus**: it asks the real GlobalISel legalizer
what it does with a query, and records exactly one outcome per atom.  There is
no frozen AArch64 reference oracle (out of scope).

## Scope

- Source: `llvm/lib/Target/AArch64/GISel/AArch64LegalizerInfo.cpp` at the
  pinned revision (`fed20e0e6832`, see `src/x86_mir_corpus/assets/aarch64-pin.json`).
  The corpus command reads the blob from that commit, not from the checkout's
  working tree.
- Driver: the same `x86-mir-run` sidecar, linked against the assertions LLVM
  build.  `mir-run` selects the AArch64 profile from the corpus.
- Outcomes: `legalized`, `failed`, `budget`, `crash`, `hang`,
  `construction_mismatch`, `unbuildable`, `not_run`.  Only the first five say
  anything about the legalizer; the last three are corpus construction.

## Feature configs

`A0`–`A3` below are **feature configs**, not evidence levels.

| Config | Features |
|---|---|
| A0 | `+neon,+fp-armv8` |
| A1 | A0 + `+fullfp16` |
| A2 | A1 + `+sve,+cssc,+rcpc3,+bf16,+lse,+mops` |
| A3 | A2 + `+outline-atomics` |

The tested baseline is A2.  `outlineAtomics` stays false there, so libcall
lowering is disabled and libcall-only rules are recorded as
`libcall_lowering_disabled`, not as legalizer failures.

## Evidence levels

- **A0 — parse/audit.**  Every constructor and predicate symbol of the pinned
  source is registered in the DSL table:
  `x86-mir-corpus dsl audit --target aarch64 --input <snapshot> --strict`.
- **A1 — single atom.**  One atom constructs and reaches an explicit outcome;
  a `--only-atom` report is evidence about that atom only.
- **A2 — witness sweep.**  The witness tier runs with production rules and
  attributes every non-`legalized` atom to a stable `detail_code`.
- **A3 — operational gate.**  No `crash`/`hang`/`budget`/`not_run` outside
  the reviewed known families.  The AArch64 family list
  (`assets/aarch64-baseline-witness.json`) is empty after the first review.

## Quotient panel

The panel was reviewed against the finite-quotient analysis
([QUOTIENT.md](QUOTIENT.md)): keys are the first-match rule index over a type
dependency component; axes are rule read sets; lengths count the parsed class
universe.  It is still a curated family list rather than mechanically emitted
from the quotient representatives; the next gap lists make that step
tractable.  The AArch64 report (`docs/quotient-aarch64-A2.json`) covers 616
rules, 364 reached by a class tuple, and 32 opaque builders.  Uncovered
quotient classes are enumerated with an example and a reason: 144
`panel_gap` and 11 `opaque_predicate`.

The boundary panel deliberately includes both `iN` and `sN` spellings (AArch64
enables extended LLT kinds), fixed and scalable vectors, `bf16`, and `f128`.
Rule witnesses add predicate-driven shapes for opaque builders, so the
witness verifies 582 of 611 active rules with 0 unprobed.  The 37 remaining
exception rows (29 distinct rules) are reviewed `pipeline_unreachable` shapes
that the pre-legalizer combiner normalises before the legalizer runs.

## Type arity

AArch64 descriptors declare more generic type indices than the source rules
constrain.  Reviewed fills extend every point to the declared arity (carry
flags, shift counts, lane masks, reduction accumulators, build-vector
elements, comparison operands, `G_IS_FPCLASS`); 11 opcodes use them.  Seven
rules declare an index that cannot be constructed (subvector index,
indexed-addressing AM, dynamic stackalloc, `bzero`) and report
`shape_arity_unreachable` with the reviewed reason.  `shape_arity` is 0.

See [ANALYSIS-AARCH64-CRASH.md](ANALYSIS-AARCH64-CRASH.md) for the crash
triage, the contract tables, and the upstream finding.

All IR-shape contracts and the scalable-constructibility additions are
**target-scoped to AArch64**.  The x86 corpus predates them and its frozen
classifications must not move; `tests/fixtures/type-contract.json` pins four
x86 controls (`G_FPTRUNC`/`G_CONCAT_VECTORS`) that these contracts would
otherwise reject.  Re-running the x86 C2 witness (2,217 atoms) on a HEAD
build and on this build gives an atom-for-atom match on outcome, detail code,
and step count; an earlier target-agnostic draft moved 33 atoms and was
reverted to this scoping.

## Corpus evolution

| Snapshot | atoms | rules verified | rules unprobed | shape_arity |
|---|---:|---:|---:|---:|
| first witness | 13,156 | 239 | 372 | 16 |
| committed witness | 13,854 | 582 | 0 | 0 |

Rule witnesses added predicate-driven shapes for opaque builders and the type
arity fills closed the missing slots; the 37 remaining rule rows carry
reviewed `coverage_exceptions` reasons (`pipeline_unreachable`, `shadowed`,
`undecided`, `no_candidate`).  The panel itself is quotient-derived (see
[QUOTIENT.md](QUOTIENT.md)), so the second snapshot is a superset, not a
re-pick.

## Results

### A2 witness (committed)

13,854 atoms.  `legalized 2,205`, `failed 2,961`,
`construction_mismatch 6,136`, `unbuildable 2,552`;
`crash/budget/hang/not_run 0`; A3 `met`.

| Detail | Count |
|---|---:|
| `type_contract` | 4,857 |
| `verifier` | 1,047 |
| `type_readback` | 232 |
| `unable_to_legalize` | 2,221 |
| `libcall_lowering_disabled` | 740 |
| `unbuildable_policy` | 1,440 |
| `immediate_rule` | 676 |
| `type_assignment_failed` | 341 |
| `shape_guard` | 91 |
| `invalid_mem_operand` | 4 |

Points: `corpora/aarch64/witness-A2.points.json`.  Report digest:
`corpora/aarch64/witness-A2.report.json`.

### A2 full (reproducible, not committed)

1,677,533 atoms (class-pair 1,074,619, boundary 600,916, rule 1,207, as 678,
opcode 113).  `legalized 20,737`, `failed 99,639`,
`construction_mismatch 959,640`, `unbuildable 597,517`;
`crash/budget/hang/not_run 0`; A3 `met`.

Measured at 8 jobs / shard 300 on the assertions build: **1:31 wall**, 7.4 GB
supervisor peak RSS.  Each driver invocation reads a pre-split 300-atom corpus
file instead of the whole 502 MB document.

Digest: `corpora/aarch64/A2-full.report.json`; the corpus and points files are
gitignored (`corpora/MANIFEST.json` records their SHA256).

## Known gaps

- **Scalable construction.**  The driver cannot build every scalable generic
  op.  Opcodes whose legalizer path uses fixed-width geometry are rejected as
  `construction_mismatch` ("scalable construction is not supported yet") and
  listed in `MIRRunner.cpp:scalableConstructible`.  Remove a name only after
  the path survives a re-run.
- **Seven declared-but-unreachable rules.**  See the type-arity table in
  [ANALYSIS-AARCH64-CRASH.md](ANALYSIS-AARCH64-CRASH.md); each carries a
  reviewed reason, never an open gap.
- **`G_SHUFFLE_VECTOR` literal construction.**  Type contracts classify
  mismatched shapes, but a well-typed shuffle still hits the MIR verifier
  ("Out of bounds shuffle index") because the driver's mask construction is
  incomplete.  This is a driver gap, not legalizer evidence.
- **Real upstream finding.**  `llvm.aarch64.neon.cls.v2i128` crashes the
  AArch64 GlobalISel legalizer on this revision (`llc -global-isel -O0`, exit
  134).  Scalar `i128` G_CTLS is not IR-reachable and is classified as
  `type_contract`; the vector repro is in
  [ANALYSIS-AARCH64-CRASH.md](ANALYSIS-AARCH64-CRASH.md).
- **Panel emission is curated.**  `unrepresented_detail` reports 144
  `panel_gap` classes the current panel misses; emitting the panel from
  quotient representatives is the next P3 increment, and would change the
  corpus point set.
- **No frozen oracle and no result-semantics check.**  AArch64 evidence is
  termination, constructibility, and explicit failure only.
- **Empty A3 family list.**  Every anomaly in a future full run is a new
  finding until a family is root-caused and reviewed.

## Reproduce

```bash
cd /home/ubuntu/x86-mir-corpus
export PYTHONPATH=src

# 0) pinned source and DSL surface
x86-mir-corpus pin verify --target aarch64 --repo "$X86_MIR_LLVM_SRC"
x86-mir-corpus dsl audit --target aarch64 --repo "$X86_MIR_LLVM_SRC" --strict

# 1) build the shared driver and check the target
x86-mir-corpus llvm build --llvm-src "$X86_MIR_LLVM_SRC" \
  --llvm-build "$X86_MIR_LLVM_BUILD" --out .build
x86-mir-corpus doctor --target aarch64 --json

# 2) A2 witness (13,854 atoms)
x86-mir-corpus corpus --target aarch64 --config A2 --tier witness \
  --repo "$X86_MIR_LLVM_SRC" --allow-coverage-gaps --oracle=none \
  --out /tmp/a64-witness.json
x86-mir-corpus mir-run --corpus /tmp/a64-witness.json \
  --driver .build/x86-mir-run --rules production \
  --jobs 8 --shard-size 300 --timeout 30 --max-steps 10000 \
  --out /tmp/a64-witness.report.json

# 3) A2 full (1.68M atoms, ~90 s at 8 jobs)
x86-mir-corpus corpus --target aarch64 --config A2 --tier full \
  --repo "$X86_MIR_LLVM_SRC" --allow-coverage-gaps --oracle=none \
  --out /tmp/a64-full.json
x86-mir-corpus mir-run --corpus /tmp/a64-full.json \
  --driver .build/x86-mir-run --rules production \
  --jobs 8 --shard-size 300 --timeout 30 --max-steps 10000 \
  --out /tmp/a64-full.report.json

# 4) regression
python3 -m unittest discover -s tests -q
python3 -m ruff check src tests scripts
```
