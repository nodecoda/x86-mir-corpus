# AArch64 A2 witness crash triage (2026-11-05)

Corpus: `corpora/aarch64/witness-A2.points.json`, 13,156 atoms, config A2.
Driver: `mir-run` built from `trees/explore` with assertions enabled.

## Before

| outcome | n |
|---|---:|
| legalized | 2,243 |
| failed | 3,526 |
| unbuildable | 3,237 |
| construction_mismatch | 3,598 |
| crash | **552** |
| budget / hang / not_run | 0 |

552 crashes over 75 opcodes. 429 involved scalable vectors, 123 did not.
The crash was fatal for the shard and forced a resume replay, so it dominated
wall time: the first witness run took 21.7 s; after the contracts, 2.2 s.

## Families

| Family | n | Trigger |
|---|---:|---|
| `LLT::getNumElements()` on scalable | 277 | scalable type reached a fixed-width legalizer path |
| `buildConstant` + scalable | 76 | constant builder rejects scalable vectors |
| scalable size conversion | 56 | `TypeSize` scalar conversion on a scalable type |
| vector lane mismatch | 47 | operands have different lane counts |
| `buildInstr` type mismatch | 46 | result/operand scalar-vector mismatch |
| element type mismatch | 21 | `isVector()`/element access on a scalar |
| invalid legality mutation | 12 | mutation shape does not match the query |
| other | 17 | widening trunc, atomic cmpxchg, unreachable |

The non-scalable crashes were role errors in the corpus type assignment, not
legalizer bugs: `G_ABDS` with floats, `G_CTLZ` result/source width mismatch,
conversions with swapped int/float roles, vector reductions with scalar
operands, `G_BUILD_VECTOR` with a non-vector result, loads whose value and
memory sizes disagree, and a 128-bit `G_ATOMIC_CMPXCHG`.

## Fix: role-aware type contracts

`sidecar/mir/MIRRunner.cpp` now checks, before building MIR:

- scalable vectors on the 65 reviewed fixed-width opcode paths;
- integer-only ABDS/ABDU, restricted to the reviewed AArch64 vector shapes
  (`v8i8`, `v16i8`, `v4i16`, `v8i16`, `v2i32`, `v4i32`);
- unary ops: result and source have the same type (slot 1 optional when the
  descriptor has a single type index);
- `FPTOSI`/`FPTOUI` (`int` result, `float` source) and `SITOFP`/`UITOFP`
  (`float` result, `int` source), same shape;
- vector reductions: scalar result whose element type matches the vector
  operand;
- build vectors: fixed vector result plus scalar elements of its element type;
- funnel shifts and `G_FLDEXP` operand roles;
- load/store value and memory sizes (extending loads and fp conversions keep
  their direction);
- `G_ATOMIC_CMPXCHG` values of 8/16/32/64 bits with a pointer address.

`LLT::operator==` folds `ANY_SCALAR` to any same-size scalar, so the contract
compares float/integer/pointer kind and size explicitly. This is what makes the
`SITOFP <8 x f16>, s32` and `VECREDUCE s32, <2 x f32>` cases visible.

## After

| outcome | n |
|---|---:|
| legalized | 1,852 |
| failed | 2,879 |
| unbuildable | 3,204 |
| construction_mismatch | 5,221 |
| crash / budget / hang / not_run | **0** |

A3: `met`. Every non-legalized atom has a `detail_code`.
The 3,394 atoms that moved from `failed`/`legalized`/`unbuildable` to
`construction_mismatch` are role-invalid combinations the legalizer used to
answer anyway; they are no longer counted as legalizer evidence.

The same contracts also remove all X86 full-corpus crashes and budgets:
`crash 3,265 → 0`, `budget 225 → 0` (208,851 atoms). The X86 points gate is
unchanged: 208,851 points, SHA256 `98ce9d09…`.

## Remaining gap

The contracts classify construction, not legalizer decisions. The next P3
input is the 30 opaque builders and 372 unprobed AArch64 rules reported by
`docs/quotient-aarch64-A2.json`.

## Round 2: rule witnesses and pipeline exclusions (2026-11-05)

The coverage work added predicate-driven rule witnesses: 13,867 atoms,
582/611 active rules verified, 24 unprobed. Running them surfaced 17 new
anomalies that split into two classes.

### Construction bugs (fixed by contracts)

| Shape | Assertion |
|---|---|
| `G_BUILD_VECTOR_TRUNC s32, s32` | `LLT::getElementType()` on a scalar |
| `G_VECTOR_COMPRESS s32, s32` | `LLT::getElementType()` on a scalar |
| `G_FCOPYSIGN s1, s32` | `buildPadVectorWithUndefElements`: source larger than result |
| `G_ATOMIC_CMPXCHG_WITH_SUCCESS s8, s32, p0` | success flag is not `i1`; SIGSEGV |

New contracts: result of `G_BUILD_VECTOR_TRUNC` is a fixed vector with scalar
integer elements; every `G_VECTOR_COMPRESS` slot is a vector of slot 0's shape;
`G_FCOPYSIGN` is a float pair of one type; `G_ATOMIC_CMPXCHG_WITH_SUCCESS` is an
8/16/32/64/128-bit value, a scalar `i1` success flag, and a pointer address.
Regression fixture: `tests/fixtures/aarch64-type-contract.json` (22 atoms).

### Pipeline-unreachable witnesses (excluded, not classified as bugs)

| Shape | Raw-query behaviour |
|---|---|
| `G_ABS v3s16` | `mutationIsSane(... "legality mutation invalid for match")` |
| `G_ADD/G_AND/G_OR/G_SUB/G_XOR/G_MUL v3s1, v5s1` | 10,001-step budget |

These are not IR-reachable at the legalizer boundary. On the pinned revision,
`llc -global-isel -O0 -mtriple=aarch64` compiles equivalent IR (load `<3 x i16>`
+ `llvm.abs`, and `add`/`mul` on `<3 x i1>`/`<5 x i1>` produced by `icmp`)
without asserting or looping: the pre-legalizer combiner widens the shapes to
`v4i16`, `v4i16`, and `v8i8` before the legalizer runs. The raw queries instead
reach `minScalarOrEltIf` rules that ping-pong on non-power-of-two lane counts.

`TargetProfile.rule_probe_exclusions` now drops those exact probe shapes and
reports each rule as `pipeline_unreachable`, never as verified. The 29 remaining
unprobed rules have reviewed entries in
`src/x86_mir_corpus/assets/coverage_exceptions.json`.

### Final A2 witness

Corpus: 13,854 atoms. `legalized 1,986`, `failed 2,982`,
`construction_mismatch 5,649`, `unbuildable 3,237`,
`crash/budget/hang/not_run 0`; A3 `met`. Coverage: 582/611 active rules
verified, 37 exception rows (29 distinct rules), 0 unprobed.

## Round 3: shape-arity closure (P4, 2026-11-05)

AArch64 opcode descriptors declare more generic type indices than the source
rules constrain: carry-out flags (`G_SMULO`/`G_UMULO`), shift counts
(`G_ROTL`, `G_CTLZ_ZERO_POISON`, `G_CTTZ_ZERO_POISON`), lane masks
(`G_VECTOR_COMPRESS`), reduction accumulators (`G_VECREDUCE_FADD`), build-vector
elements (`G_BUILD_VECTOR_TRUNC`), three-way comparison operands
(`G_SCMP`/`G_UCMP`), and the classified value (`G_IS_FPCLASS`). Rule probes only
carried the slots a predicate referenced, so 16 builders still reported
`shape_arity`: their opcode arity exceeded the rule's slot count and every point
was unbuildable.

### Reviewed fills

`TargetProfile.type_fills` maps `opcode -> {type index: fill kind}`. Each fill
is derived from the descriptor's TableGen type layout on the pinned revision and
is applied to indices the rule does not constrain; constrained slots keep the
probe value.

| Opcode | Index | Fill |
|---|---|---|
| `G_SMULO`, `G_UMULO` | 1 | `i1of0` — carry-out with the result's shape |
| `G_ROTL`, `G_CTLZ_ZERO_POISON`, `G_CTTZ_ZERO_POISON`, `G_FCOPYSIGN` | 1 | `same0` — same type as slot 0 |
| `G_BUILD_VECTOR_TRUNC` | 1 | `elem0` — vector element type |
| `G_VECREDUCE_FADD` | 2 | `same0` — accumulator of the result type |
| `G_VECTOR_COMPRESS` | 1 | `i1vec0` — `<N x i1>` lane mask |
| `G_IS_FPCLASS` | 1 | `f32shape0` — float with the result's shape |
| `G_SCMP`, `G_UCMP` | 1 | `operand1` — integer operand vector |

Fills preserve scalable element counts (`nxv16i8 -> nxv16s1`), so the boundary
panel keeps its scalable rows instead of collapsing them to fixed vectors.

### Declared-but-unreachable rules

Seven rules declare a type index the corpus cannot construct: `G_INSERT_SUBVECTOR`
(subvector index), the four `G_INDEXED_*` memory ops (indexed-addressing AM
immediate), `G_DYN_STACKALLOC` (unbuildable policy), and `G_BZERO` (libcall
lowering disabled + tailcall immediate). They keep their reviewed reason in
`TargetProfile.shape_arity_unreachable` and are reported as
`shape_arity_unreachable`, never as open construction gaps.

Result: AArch64 `shape_arity 16 -> 0`; 7 `shape_arity_unreachable`.

### Driver gaps the fills exposed

All are construction contracts; none is a legalizer decision.

- `G_ROTL` had no funnel-shift contract (only `G_ROTR`/`G_FSHL`/`G_FSHR` did).
  Float shapes reached `AArch64LegalizerInfo.cpp:192` and asserted in
  `getNumElements()`. `G_ROTL` now shares `FunnelShiftRules`.
- Scalable construction rejects `G_CTLZ_ZERO_POISON`, `G_CTTZ_ZERO_POISON`,
  `G_ROTL`, `G_SCMP`, `G_UCMP`, `G_SMULO`, `G_UMULO`, and `G_IS_FPCLASS`.
  `G_IS_FPCLASS` asserted in `MachineIRBuilder::buildConstant` ("unexpected
  scalable vector"); the rest hit scalable `getNumElements()`/size paths.
- `fixedBits` and the contract pre-pass now guard `TypeSize` with
  `isVector() && isScalable()` before `getFixedValue()`.

### A2 witness after the arity work

13,854 atoms. `legalized 2,250`, `failed 2,986`,
`construction_mismatch 5,977`, `unbuildable 2,641`;
`crash/budget/hang/not_run 0`; A3 `met`. Coverage is unchanged at 582/611
active rules verified, 37 exception rows, 0 unprobed.  Round 4 tightened the
contracts again; the final witnessed counts are there.

## Round 4: A2 full and IR-shape contracts (P5, 2026-11-05)

The A2 full corpus crosses every class-pair and the whole boundary panel per
rule: **1,677,533 atoms** (classpair 1,074,619; boundary 600,916; rule 1,207;
as 678; opcode 113).  The witness did not exercise these combinations, so the
first full run surfaced 5,609 crashes.  None was a legalizer decision: every
one was a shape no IR producer can form, plus scalable shapes the driver
cannot construct.

### Runner: pre-split shards

Each driver invocation used to re-parse the whole corpus.  For 1.68M atoms the
LLVM JSON DOM costs ~19 GB per process, so 8 jobs exceeded the machine.  The
supervisor now writes a compact per-shard corpus (300 atoms) and passes
`X86_LEGALIZER_MIR_INDEX_BASE` so markers keep their global index.  Peak
supervisor RSS is 7.4 GB (from ~19 GB per driver), and the run takes 1:31 at
8 jobs / shard 300.

### Contracts added

| Shape family | Contract |
|---|---|
| `G_SHL`/`G_LSHR`/`G_ASHR` | both operands the same integer type |
| `G_CONCAT_VECTORS` | source and result share the element type; result has twice the lanes |
| `G_INSERT_VECTOR_ELT` | `(dst:type0, src:type0, elt:type1, idx:type2)`; element is slot 0's element, index is a scalar integer |
| `G_EXTRACT_VECTOR_ELT` | result is the vector's element, index is a scalar integer |
| `G_BITCAST` | equal total bit width; no scalable/fixed mix |
| `G_SHUFFLE_VECTOR` | result and source are the same vector type |
| `G_FPTRUNC` | both float, result strictly narrower |
| `G_VECREDUCE_SEQ_FADD`/`FMUL` | float result, same accumulator, vector of that element |
| `G_CTLS` | integer lanes no wider than 64 bits |

The whole table above is enforced only when the corpus declares target
`aarch64` (`aarch64OnlyContract` in the driver); the x86 corpus keeps its
pre-existing classifications, and the scalable list likewise has a frozen
shared base plus an AArch64-only addition.

Scalable construction stays unsupported for the opcodes whose lowering uses
fixed-width geometry: `G_CONCAT_VECTORS`, `G_BITCAST`, `G_SHUFFLE_VECTOR`,
`G_CTLZ`, `G_CTPOP`, `G_CTTZ`, `G_ROTR`, `G_CTLS`, `G_VECREDUCE_SEQ_*`, and the
shifts.  These are `construction_mismatch` with the existing "scalable
construction is not supported yet" reason, never crashes.
`G_INSERT_VECTOR_ELT`/`G_EXTRACT_VECTOR_ELT` are no longer excluded: once the
type contracts above fix the element/index types, scalable forms build and
legalize or fail explicitly (8 atoms moved from `construction_mismatch` to
`failed` in the full run, still zero crashes).

### Real upstream finding (not constructor-fixable)

`llvm.aarch64.neon.cls` requires a vector type at the IR level, so scalar
`i128` G_CTLS is not IR-reachable (classified as `type_contract`).  The
**vector** form is reachable and crashes the AArch64 GlobalISel legalizer on
the pinned revision:

```llvm
define <2 x i128> @f(<2 x i128> %x) {
  %r = call <2 x i128> @llvm.aarch64.neon.cls.v2i128(<2 x i128> %x)
  ret <2 x i128> %r
}
```

```text
llc -global-isel -O0 -mtriple=aarch64
# assertion/stack dump, exit 134
```

The corpus records scalar i128 as `type_contract`; the vector form is written
up here for an upstream report rather than hidden in the corpus.

### Final A2 full

`legalized 20,737`, `failed 99,639` (unable-to-legalize 97,636 + libcall
lowering disabled 2,003), `construction_mismatch 959,640` (`type_contract`
633,983, `verifier` 318,421, `type_readback` 7,236), `unbuildable 597,517`;
`crash/budget/hang/not_run 0`; A3 `met`.  Coverage is the same 582/611 active
rules.  The digest is `corpora/aarch64/A2-full.report.json`; the 502 MB corpus
and 432 MB points stay out of git.

### Final A2 witness (re-measured)

13,854 atoms: `legalized 2,205`, `failed 2,961`,
`construction_mismatch 6,136` (`type_contract 4,857`, `verifier 1,047`,
`type_readback 232`), `unbuildable 2,552`; `crash/budget/hang/not_run 0`; A3
`met`.  The 159 atoms that moved out of `legalized`/`failed`/`unbuildable` are
the IR-shape violations above, now classified before construction.
