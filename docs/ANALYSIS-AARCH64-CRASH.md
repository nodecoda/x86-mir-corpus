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
