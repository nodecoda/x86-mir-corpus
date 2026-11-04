# x86-legalizer-corpus

Build legalizer query corpora from the **hand-written** X86 legalizer C++
(`llvm/lib/Target/X86/GISel/X86LegalizerInfo.cpp`), at any git revision
(default `upstream/main`), then let an oracle fill in the expected answers.

This tool is deliberately not a TableGen reader.  It parses the source file
that a human wrote, so the corpus describes the chain semantics that the
generated table is supposed to replace.  Generated `.inc` files are read by
`verify` only, as a cross-check.

## Oracle boundary

The Python side never writes `expected`.  It emits query shapes only:

```
C++ source --parse--> rule ledger --points--> query shapes
                                                  |
                              X86Tests (reference) fills the answers
                                                  |
                                            corpus JSON
```

The only producer of an expected action is
`X86LegalizerInfoReference::getAction()` in the C++ unit test driver.
`points` output is therefore safe to diff and review: it contains no answers
that the table could have influenced.

## Source and oracle

The tool separates *where the shapes come from* from *where the answers come
from*:

| Piece | Default | Meaning |
|---|---|---|
| source | `--source-rev upstream/main` | the C++ file the query shapes come from |
| oracle | `none` by default; `reference` when `--build` is given | the only producer of `expected` |
| coverage exceptions | `coverage_exceptions.json` | reviewed reasons for rules that cannot be probed |

`--oracle=none` writes a shapes-only corpus: every atom is present, none has
an `expected` field.  `--oracle=reference` runs the frozen C++
`X86LegalizerInfoReference::getAction()` through `X86Tests` and is the only
mode that fills `expected`.  A revision-specific `upstream-build` oracle is
not implemented; that is why the default for `upstream/main` is
`--oracle=none`.

`source.rev` records the requested spelling (`upstream/main`), while
`source.revision` records the resolved 40-character commit.  The oracle
records the resolved reference commit and, when it differs from the requested
spelling, its `spec`.

If `source.revision != oracle.revision` the corpus is cross-revision: the
envelope carries `"oracle": {"cross_revision": true, ...}` and
`--oracle=reference` refuses to run unless `--allow-cross-revision` is given.
With the flag, `<out>.report.json` records the rule delta by
`(rule_set, method, argument text)`, not by rule index.

## Quick start

```bash
# 1. Shapes from upstream/main, with no expected answers.  This is the
#    default workflow for a revision that has no matching oracle build.
python3 llvm/utils/x86-legalizer-corpus corpus \
    --source-rev upstream/main --oracle=none --tier witness \
    --out /tmp/up-shapes.json

# 2. Rule ledger for the same revision.
python3 llvm/utils/x86-legalizer-corpus parse \
    --source-rev upstream/main --config C2 --out /tmp/up-ast.json

# 3. Cross-check the hand-written chain against a TableGen emission.
python3 llvm/utils/x86-legalizer-corpus verify \
    --ast /tmp/up-ast.json \
    --td-inc build/unittests/Target/X86/X86LegalizerRuleIndex.inc \
    --out /tmp/up-report.json

# 4. Corpus with expected answers from the frozen C++ reference.
#    The source revision must match the reference unless you opt in.
python3 llvm/utils/x86-legalizer-corpus corpus \
    --source-rev ebac17721d12 --tier witness \
    --build /path/to/build --out /tmp/x86-corpus.json
```

`corpus` without `--build` writes only the points file (`<out>.points.json`)
and tells you what to do next.  Environment overrides for step 4 are
`X86_LEGALIZER_CPP_POINTS` and `X86_LEGALIZER_CPP_CORPUS_OUT`; they select the
input points and the output corpus for the gtest driver.

Step 3 exits 3 on `upstream/main`: TableGen has two `G_CTPOP` rules that the
main-branch C++ chain does not have (`HasVPOPCNTDQ`, `HasVPOPCNTDQVL`).  That
is the tool reporting a real source delta, not a corpus bug.

## Key -> result traces

`corpus` records one legalization decision per query: key -> action.  A
legalizer also cares about what happens next: apply the mutation, ask again,
repeat.  Two disabled gtest drivers replay a corpus that way, with the same
loop and a different oracle:

| Driver | Oracle |
|---|---|
| `*TraceCorpusWithReference*` | frozen C++ `X86LegalizerInfoReference` |
| `*TraceCorpusWithTable*` | `X86LegalizerTable` |

Both read `X86_LEGALIZER_TRACE_CORPUS_IN` and write
`X86_LEGALIZER_TRACE_CORPUS_OUT`; `X86_LEGALIZER_TRACE_MAX_ROUNDS` (default
64) is the cap after which a still-unresolved atom is reported as `cap`.

Each trace document records, per atom, the start state, the terminal `status`
(`legal`, `unsupported`, `custom`, `lower`, `libcall`, `cycle`, `cap`,
`invalid`), the final types and memory descriptor, and every step
(`action`, `idx`, `to`).  Table traces also record each step's `answer_kind`
(`hit`, `residual`, `escape`, `outside`, `untabulated`).

The simulation applies only the type mutation named by the step.  Actions that
carry no type (Legal, Unsupported, Custom, Lower, Libcall) end the trace,
memory descriptors stay fixed, and a repeated `(types, memdesc)` state is
reported as `cycle`.  The simulator never invents an answer: every `status`
comes from the oracle under test.

One corpus, both oracles, one comparison:

```bash
llvm/utils/x86-legalizer-corpus/run-traces.sh \
    /tmp/x86-corpus.json /tmp/traces /path/to/build
```

It runs both drivers and then `trace-diff --strict`, which exits 1 on any
difference.  The same steps by hand:

```bash
# reference trace
X86_LEGALIZER_TRACE_CORPUS_IN=/tmp/x86-corpus.json \
X86_LEGALIZER_TRACE_CORPUS_OUT=/tmp/trace-reference.json \
  build/unittests/Target/X86/X86Tests --gtest_also_run_disabled_tests \
    --gtest_filter='*TraceCorpusWithReference*'

# table trace over the same corpus
X86_LEGALIZER_TRACE_CORPUS_IN=/tmp/x86-corpus.json \
X86_LEGALIZER_TRACE_CORPUS_OUT=/tmp/trace-table.json \
  build/unittests/Target/X86/X86Tests --gtest_also_run_disabled_tests \
    --gtest_filter='*TraceCorpusWithTable*'

# compare key -> result
python3 llvm/utils/x86-legalizer-corpus trace-diff \
    --left /tmp/trace-reference.json --right /tmp/trace-table.json \
    --out /tmp/trace-diff.json --strict
```

`trace-diff` joins by atom id, compares `rounds`, `status`, `final_types`,
`final_mem`, and the step list, and reports status/rounds histograms plus the
first divergent step for each difference.  `answer_kind` is provenance: it is
summarised, never counted as a difference.

## End-to-end MIR

The corpus, differential, and trace drivers decide what a *query* becomes.
This runner decides whether the real legalizer *stops*: each atom becomes a
minimal `MachineFunction`, is verifier-gated, then runs
`legalizeMachineFunction` with a step-budget observer.  Every atom gets a
fresh `Module`, `Function`, `MachineModuleInfo`, and `MachineFunction` (never
reuse a cached MF), one MBB with one target instruction, `G_IMPLICIT_DEF`
sources, and a `COPY` liveness anchor when the defs would be trivially dead.
`MF.verify()` must pass; a verifier failure is `construction_mismatch` and
the legalizer is not run.  `StepBudgetObserver` counts
`createdInstr + changingInstr + erasingInstr` and calls `report_fatal_error`
above `--max-steps` (default 10000), naming the atom on stderr; the
supervisor adds an external timeout for loops that never touch an MI.
Termination is the only judgement.  Synthetic operands have no semantics.

The three earlier layers miss artifact-level livelocks.  A `v2s32`
`G_BUILD_VECTOR` atom says `MoreElements`; differential and trace agree, and
the trace advances to `v4s32`.  The real helper appends undef lanes
(`moreElementsVector()`), then `buildDeleteTrailingVectorElements()` deletes
them and rebuilds a `v2s32` `G_BUILD_VECTOR`, so the next query is the same
`v2s32`, forever.  The loop lives in `query -> helper -> artifact`, below the
simulated state machine.

Build and run the disabled driver through `mir-run`:

```bash
ninja -C build X86Tests

python3 llvm/utils/x86-legalizer-corpus/__main__.py mir-run \
    --corpus up-C2.json \
    --build build \
    --rules production --shard-size 2000 --timeout 120 --max-steps 10000 \
    --jobs 4 --out mir-C2.json
```

`mir-run` resolves `build/bin/X86Tests` or
`build/unittests/Target/X86/X86Tests`, runs
`--gtest_also_run_disabled_tests --gtest_filter='*RunCorpusThroughLegalizer'`,
and sets `X86_LEGALIZER_MIR_CORPUS_IN` (input), `X86_LEGALIZER_MIR_OUT`
(JSONL), `X86_LEGALIZER_MIR_MAX_STEPS` (default 10000),
`X86_LEGALIZER_MIR_SHARD` (`i/N`, default `0/1`), and
`X86_LEGALIZER_MIR_RULES` (`production` or `reference`).

Sharding uses `N = ceil(atoms / --shard-size)`; shard `i` owns
`global_index % N == i`.  `--jobs` runs shards concurrently and merges in
corpus order.  `--only-atom ID` runs a temporary one-atom corpus as `0/1`,
keeps the original atom count, and marks the rest `not_run` (`only-atom`).

The driver also reads `X86_LEGALIZER_MIR_START_INDEX` (global 0-based atom
index, default 0) and begins at that atom.  Shards are resume-capable: with
`--shard-size 2000 --jobs N`, if a shard hits budget, hang, or crash, the
supervisor restarts the same shard after that atom via `START_INDEX`,
preserving the anomaly result and continuing with the remaining atoms.  Any
stall that still makes no progress is reported as `not_run`, so the report
stays honest.  A process that ends after the last complete outcome but before
the next atom writes its `start` marker is not evidence about that atom: it is
reported as `not_run` (`timeout_before_start` on a timeout,
`unattributed_termination` otherwise), never as a `crash`, and the resume loop
retries it from that index.  The driver opens its JSONL before parsing the
corpus; if a shard times out before any marker (a slow corpus load, not
evidence about an atom), the supervisor retries once with a six-times longer
deadline and only then reports `not_run` with reason `timeout_before_start`
(exit 2).  The longer deadline is one-shot: the next invocation that makes
progress returns to the configured timeout.
Missing/invalid environment or unreadable corpus prints
`X86_LEGALIZER_MIR_INFRA_ERROR` and exits 2, never a gtest skip.

The report is `llvm-x86-legalizer-mir-run` version 1:

```json
{
  "schema": "llvm-x86-legalizer-mir-run", "version": 1,
  "corpus": {"path": "...", "sha256": "...", "config": "C2", "atoms": 2217},
  "rules": "production",
  "budget": {"max_steps": 10000, "timeout_s": 120, "shard_size": 2000},
  "counts": {"legalized": 388, "failed": 1034, "budget": 20, "hang": 0,
             "crash": 24, "unbuildable": 447, "construction_mismatch": 304,
             "not_run": 0},
  "first_anomaly": {...}, "atoms": [{"id": "...", "outcome": "budget", "steps": 10001}]
}
```

It also carries `a3`, `anomalies`, `new_findings`, `known_anomalies`,
`coverage_gaps`, `libcalls: "disabled"`, `only_atom`, `infra_warnings`,
`unbuildable_reasons`, and `shard_summaries`; `sum(counts) == corpus.atoms`.
`first_anomaly` is the first observed atom that is not `legalized`/`failed`
(it may be `unbuildable`, `construction_mismatch`, `budget`, `hang`, or
`crash`); `not_run` placeholders from `--only-atom` or an unresolved stall
are not observations and are skipped.

Exit 0 means every atom is terminal (`legalized`, `failed`, `unbuildable`,
`construction_mismatch`); exit 1 means some `budget`, `hang`, or `crash`;
exit 2 is infrastructure, including a corpus/config pointer-width mismatch.
`not_run` following an anomaly keeps that anomaly's exit 1.  `--only-atom`
decides the exit from the atom actually run.  A timeout before the `legalize`
marker (`slow_or_stuck_before_legalizer`), a pre-start timeout
(`timeout_before_start`), and an unattributable termination between atoms
(`unattributed_termination`) are incomplete evidence and exit 2, as does any
other unexplained `not_run`.

### Observed witness baseline

The committed baseline is `mir-baseline-witness.json`.  It is **not clean**:

| Config | Atoms | legalized | failed | unbuildable | construction_mismatch | budget | crash |
|---|---|---|---|---|---|---|---|
| C0 | 2062 | 170 | 1119 | 441 | 303 | 5 | 24 |
| C1 | 2169 | 316 | 1059 | 445 | 304 | 21 | 24 |
| C2 | 2217 | 388 | 1034 | 447 | 304 | 20 | 24 |
| C3 | 2093 | 216 | 1103 | 442 | 303 | 5 | 24 |

Every config has exactly 5 known `X86_BUILD_VECTOR` `MoreElements` atoms that
reach `budget`; those are the A3 family, not regressions.  Non-family budgets
are 0 / 16 / 15 / 0 (C0 / C1 / C2 / C3), and every config has 24 crashes (all
`crash:assert`).  **A3 is NOT MET** because the unfixed production reports
contain those non-family budgets and crashes; the baseline records
`"a3_met": false`.  The 4 `G_UADDE` `[v2p0:*, p0:*]` atoms per config pass
the verifier, then assert inside the legalizer; they are among the 24
`crash:assert` results, not reclassified.  All crashes are recorded anomalies
on synthetic MIR, not confirmed legalizer bugs.  Control flow is out of scope:
15 `G_BR` and 15 `G_BRCOND` atoms per config are `unbuildable`, as are the 5
`G_PHI` `MoreElements` atoms per config.  Crashes are recorded, not fixed.
The baseline also records the per-config `MoreElements` census
(49/49/48/49 expected-action atoms, 5 `G_PHI` each), a
`construction_mismatch` breakdown by opcode and stable detail code, the
unbuildable reasons, and sorted atom ids for every anomaly bucket, so a
count change is attributable instead of opaque.

Every `construction_mismatch` is a verifier rejection of a corpus shape that
cannot exist in real MIR: pointers in `G_ANYEXT`/`G_SEXT`/`G_FPEXT`, pointer
`G_PTR_ADD` offsets, load/store sizes that do not match the result, a
`G_EXTRACT` whose source is not larger, and mixed scalar/vector
`G_IS_FPCLASS`.  The legalizer is not run for those atoms.

### A1 / A2 evidence

```bash
# A1: expect outcome=budget, steps=10001, exit 1.
python3 llvm/utils/x86-legalizer-corpus/__main__.py mir-run \
    --corpus up-C2.json --build build \
    --rules production --shard-size 100000 --timeout 120 --max-steps 10000 \
    --only-atom cpp/X86_BUILD_VECTOR/G_BUILD_VECTOR/rule/3 \
    --out mir-A1-C2.json

# A2: temporary fixed rules; checks failed, restore, budget, clean .td.
/home/ubuntu/llvm/trees/explore/llvm/utils/x86-legalizer-corpus/mir-a2-probe.sh \
    --tree "$PWD" --build build --corpus up-C2.json \
    --atom cpp/X86_BUILD_VECTOR/G_BUILD_VECTOR/rule/3
```

A2 checks `failed|legalized` on the patched run (on the frozen revision the
probe atom ends `failed`; the report lands in
`--evidence-dir`/mir-A2-C2.json), restores the `.td`, rebuilds production,
re-requires the A1 atom to be `budget` (steps `max_steps + 1`), and requires a
clean target file.  The patch is a **temporary C2 TableGen proxy for
PR #238277**, not the merged C++ fix; the upstream commit changes
pre-TableGen C++ and does not apply here.

A5: run one config twice and diff only per-atom
`{id, outcome, steps, detail_code}`; timing and `stderr_tail` are excluded.
A6: C0/C3 use 32-bit pointers and C1/C2 64-bit.  A corpus/config width or
config-ID mismatch is rejected before per-atom work with exit 2, never as
`construction_mismatch`.  `mir-baseline-witness.json` is a deterministic
projection of four witness reports (sorted atom ids, no timestamp,
`sort_keys=True`, counts, MoreElements census, A3 buckets,
construction_mismatch breakdown, unbuildable reasons, and out-of-scope
counts).  Regenerate with `--shard-size 2000 --jobs 4 --timeout 30` (see
`provenance.regenerate_reports`), then re-project the fields listed in
`provenance.regenerate_baseline`.

The fixture class is `X86LegalizerMIRTest`, not
`X86LegalizerDifferentialTest`: the differential fixture is anonymous-namespace
in another TU and gtest rejects two fixture classes under one suite name.  The
test name is unchanged, so `--gtest_filter='*RunCorpusThroughLegalizer'`
still selects exactly this driver.

## Tiers

| Tier | What it adds | Points on the frozen revision (C2) |
|---|---|---|
| `witness` | reviewed 12-point boundary panel, address-space probes, per-rule witnesses | 2219 |
| `full` | every class pair over the C++ class axis, plus the witness panel | 208853 |
| `deep` | `full` plus the reviewed 60-point deep panel | 319541 |

Tiers only change the query shapes.  They never change the expected answers,
which always come from the oracle.

`full` and `deep` prune class-pair and boundary shapes that put a pointer (or
pointer vector) in a slot targeted by an element-size mutation that can match
vectors of pointers (`clampScalarOrElt`, `scalarSameSizeAs`,
`min/maxScalarOrElt`).  The reference applies the first match's mutation, and
`LLT::changeElementSize` rejects pointers with an assertion, so those are not
well-formed legalizer queries.  The points document records the count under
`"pruned": {"pointer_element_size": N}` (858 for `full`, 2010 for `deep` on
the frozen revision).  This is a query-shape safety filter, not an oracle: it
never writes an expected action.

## Configs

`--config` selects a row of the `CONFIGS` table in `target.py`; the rows mirror
the four `FullConfigs` in `X86LegalizerInfoDifferentialTest.cpp`.

| Config | Shape |
|---|---|
| `C0` | 32-bit, no SSE/AVX, no x87 |
| `C1` | 64-bit, SSE2 only |
| `C2` | 64-bit, AVX-512 + BWI/DQI/VLX (witness config; every guard is taken) |
| `C3` | 32-bit with POPCNT/LZCNT/BMI, no SSE |

`verify` currently accepts only a `C2` AST.  The emitted rule index is the
unconditional TableGen translation, while `C0`/`C1`/`C3` prune guarded rules;
comparing another config would report every pruned rule as a delta.

## What the reader understands

The parser is a strict subset of C++, not a general interpreter:

- declarations of `LLT` aliases and integer constants;
- nested ternaries, arithmetic, comparisons and `Subtarget.<predicate>()`;
- `if`/`else` with literal pruning, including `auto &Action = ...` chains;
- `for (unsigned Op : {G_...})` expansion;
- the builder chain methods listed in `KNOWN_METHODS`;
- lambdas are recorded, not executed; simple literal predicates inside a
  lambda (`typeIs`, `typeInSet`, `typePairInSet`, `scalar*Than`, ...) are
  modelled to find a witness, and anything else stays unverified until it has
  a reviewed coverage exception.

Anything else is a hard error with a line number.  `--allow-opaque` downgrades
an unknown chain method to a recorded warning; use it only when you are
inspecting a new source revision, never for a committed corpus.

## Extending the tool

- **New chain method**: add it to `KNOWN_METHODS`, teach `record_shape()` which
  slots it reads, and add a case to `domain.probe_candidates()` and
  `domain.rule_matches()`.  The point of the ledger is that a silent no-op is
  impossible.
- **New lambda predicate**: extend `domain.pred_witness()` /
  `probe_from_lambda()` for a literal predicate, or record a reviewed
  exception.  A rule whose predicate cannot be modelled is never silently
  dropped.
- **New `Subtarget` predicate**: add it to `target.FEATURE_KEYS` and to every
  `CONFIGS` row.  The parser fails if a guard mentions a predicate that the
  feature environment does not define.
- **New LLT spelling**: extend `expr.llt_name()` / `llt_bits()`; the corpus
  format uses those strings, so a rename is a corpus-format change.
- **New boundary panel**: change `target.BOUNDARY_PANEL` or `DEEP_PANEL`.
  These are review constants, intentionally not derived from the source.

## Coverage manifest

Every points/corpus document carries a coverage manifest, and both commands
exit 3 when a rule has no verified probe and no reviewed exception:

```json
"coverage": {
  "rules": {
    "source_total": 277, "active": 273, "verified": 263,
    "probed_candidates": 263,
    "inactive": [{"rule_set": "X86_FNEG", "rule_index": 0,
                  "reason": "feature predicate false"}],
    "excepted": [{"rule_set": "...", "rule_index": 3, "reason": "..."}],
    "unprobed": []
  },
  "opcodes": {
    "total": 115, "multi_opcode_builders": 19, "secondary_opcodes": 53,
    "per_opcode_atoms": {"X86_EXTRACT": {"G_EXTRACT": 17, "G_INSERT": 18}},
    "per_opcode_shape_atoms": {"X86_EXTRACT": {"G_EXTRACT": 17,
                                               "G_INSERT": 17}},
    "uncovered": []
  },
  "policy": "..."
}
```

- `verified` means the candidate shape is the **first match** under the
  modelled chain predicates, not merely that the rule's own predicate is
  true.
- A builder such as `getActionDefinitionsBuilder({G_EXTRACT, G_INSERT})` gets
  rule, boundary, and address-space probes for **every** opcode.  Lambdas that
  branch on `Query.Opcode` are evaluated per opcode, including lambda-local
  variables such as `SubIdx = Query.Opcode == G_EXTRACT ? 0 : 1`.
- `per_opcode_atoms` counts every origin; `uncovered` and
  `per_opcode_shape_atoms` count only `rule`, `boundary`, and `as` atoms, so
  an opcode that only has an `opcode` probe is still reported as a gap.
- `--allow-coverage-gaps` downgrades the exit-3 gate for inspection only; do
  not use it for a committed corpus.

### Registering a coverage exception

1. Run `points`/`corpus`; exit 3 lists every unprobed rule.
2. Confirm why it cannot be probed: unreachable under the preceding clamps,
   shadowed by an earlier match, or an opaque lambda with no literal witness.
3. Add an entry to `coverage_exceptions.json`:

   ```json
   {"rule_set": "X86_ADD", "rule_index": 9, "method": "clampMinNumElements",
    "configs": ["C0", "C3"],
    "reason": "a one-element fixed vector is a scalar; earlier clamps win"}
   ```

   Matching fields are `rule_set` (required), `rule_index`, `method`, `arg`,
   `arg_contains`, and `config`/`configs`.
4. The reason must describe the source semantics, not "the test fails".  An
   exception is reviewed together with the code change that needs it.

## Comparing corpora

`diff` joins two corpora on the canonical query key
`(config fingerprint, opcode, types, memory type, align bits)`.  It never
joins on atom ids, array order, or raw bytes:

```bash
python3 llvm/utils/x86-legalizer-corpus diff \
    --left /tmp/up-corpus.json --right /tmp/ref-corpus.json \
    --out /tmp/diff.json
```

The report contains `expected_diff`, `left_only`, `right_only`,
`per_rule_coverage`, and `unmapped_rule_sets`.  `expected` is compared only
where both sides carry one: a shapes-only corpus against a filled corpus
reports `expected_available: false`, never a false "0 differences".
`--strict` exits 1 on any difference.

Version-1 documents are read as well.  They carry no `opcode`; when the other
side maps a rule set to exactly one opcode, that mapping is used.  A
multi-opcode builder that cannot be resolved is listed in
`unmapped_rule_sets` instead of being guessed.

Cross-revision example: `upstream/main` corpus vs the frozen-reference corpus
has `expected_diff: []` over the 2133 common query keys and two
`right_only` probes, the `G_CTPOP` rules gated by `HasVPOPCNTDQ` and
`HasVPOPCNTDQVL`.  That is the intended signal: the C++ source moved, the
shared behavior did not.

Shapes-only and filled corpora share one atom shape:
`{"id", "origin", "rule_set", "opcode", "rule_index", "config", "query":
{"types": [...], "mem": {"type", "align"}}}`, where pointers carry their bit
width (`p0:64`, `v2p0:64`).  The intermediate points file used by the C++
filler keeps the short spellings and top-level `types`/`mem`/`align`.

### Migrating a v1 artifact

The committed witness is version 1 (`llvm.gisel.x86-legalizer-witness`).  Do
not rewrite it.  Generate a v2 copy next to it when a tool needs the new atom
shape:

```bash
python3 llvm/utils/x86-legalizer-corpus migrate \
    --input llvm/unittests/Target/X86/x86-legalizer-witness.json \
    --out /tmp/x86-witness-v2.json
```

`migrate` takes the rule-set -> primary-opcode map from the document's
`source_commit`; if that revision does not contain the hand-written chain
(the current v1 witness was generated from the TableGen path), it falls back
to the frozen reference and records which source it used.  The migrated
document keeps its per-atom `config` and does not synthesize a feature map;
regenerate points with `corpus` if you need to fill expected answers.

### Nightly drift check

`upstream-nightly.sh` runs the pipeline against `upstream/main`:

```bash
llvm/utils/x86-legalizer-corpus/upstream-nightly.sh /tmp/x86-nightly
```

It fails when the reader cannot parse the source or when `points` reports an
unregistered coverage gap.  The `diff` against the committed witness is
informational: a cross-revision query-domain difference (today the CTPOP
delta) is expected and does not fail the job.

## Deltas and verification

`verify` compares the C++ rule ledger with the TableGen-emitted rule index and
classifies every difference:

| Class | Meaning |
|---|---|
| `match` | same rule count |
| `td_expands_lambda` | TableGen expanded one predicate over several classes |
| `td_only` | TableGen has rules the C++ chain did not record |
| `input_missing` | the C++ chain has more rules than TableGen |
| `no_cpp_builder` | a TableGen row has no builder in the source |

Every non-`match` class must be registered in `known_deltas.json` as a
`(rule_set, class)` pair with a reason.  The default registry is read
automatically; pass `--known-deltas` to point at another file.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | every delta is registered |
| 2 | the source has an unknown chain method (use `--allow-opaque` to inspect) |
| 3 | at least one delta is not registered |

Running `verify` against `upstream/main` is a useful negative test: the main
branch has two fewer `G_CTPOP` rules, so the run exits 3 with
`X86_CTPOP:td_only`.  That is the tool detecting a real input difference, not
a corpus bug.

## Determinism and tests

Same input, same bytes: every collection is sorted by a stable key before JSON
emission, and points are serialized with `sort_keys=True`.

```bash
python3 -m unittest discover -s llvm/utils/x86-legalizer-corpus/tests -v
```

The suite covers the lexer, the expression evaluator, branch/loop expansion,
the frozen-reference census, point determinism, the oracle boundary, the
coverage manifest, corpus comparison, v1 migration, and the delta registry.  Tests that need the frozen revision or a built
`X86LegalizerRuleIndex.inc` skip when those artifacts are absent.

Frozen-revision census (a change here means the reader or the source moved):

```
call_sites=58  builders=62  opcodes=115  classes=24  mem_classes=23  aligns=1
chain_rules=277  tablegen_rows=63
```
