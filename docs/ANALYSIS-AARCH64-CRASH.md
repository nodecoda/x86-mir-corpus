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
