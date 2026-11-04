# Evidence contract

This file fixes the semantics that downstream evidence may rely on.

## Schemas

| Document | Schema | Version |
|---|---|---|
| Parsed corpus / filled corpus | `llvm-x86-legalizer-cpp-corpus` | 2 |
| MIR report | `llvm-x86-legalizer-mir-run` | 1 |
| Baseline projection | `llvm-x86-legalizer-mir-baseline-projection` | 1 |
| Triage clusters | `x86-legalizer-mir-triage` | 1 |
| Triage verification | `x86-legalizer-mir-triage-check` | 1 |
| Reference provenance | `llvm-x86-legalizer-reference-provenance` | 1 |

## Atoms and outcomes

Every corpus atom carries `id`, `query.types`, optional `query.mem`,
`expected.action`, and `config`.  A MIR report has one result per atom with an
explicit `outcome`:

`legalized`, `failed`, `budget`, `crash`, `hang`, `construction_mismatch`,
`unbuildable`, `not_run`.

`failed` carries `failed_on_opcode` (the opcode reported by the legalizer) and
`detail_code` (`unable_to_legalize` or `libcall_lowering_disabled`).
`construction_mismatch` carries `detail_code` `type_readback` (the atom asked
for more/other types than the descriptor has), `verifier` (the built MIR failed
`MachineFunction::verify`), or `type_contract` (a type slot violates the
generic opcode contract, e.g. a pointer carry or a non-i1 `icmp` result).  A
`type_contract` outcome is decided before the instruction is built: `steps` is
0 and the legalizer never runs.
`unbuildable` always carries a non-empty reason.  `not_run` is never folded
into success; a missing result is reported as `not_run` with
`missing_result`.

## Conservation and determinism

- `sum(counts.values()) == corpus.atoms` for a completed report.
- No wall-clock timestamps in reports, baselines, or triage output.
- JSON is written with sorted keys where the schema is a document (reports,
  baselines, triage); two identical runs are byte-identical.
- Corpus atom ids are stable; the corpus SHA-256 is recorded in the report.

## Exit codes

| Command | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| `parse`/`points` | ok | — | unknown chain method | coverage gap |
| `corpus` | ok | — | unknown method / bad input | coverage gap |
| `mir-run` | A3 met | report written, A3 not met | infrastructure error | — |
| `baseline check` | unchanged | added/removed atoms | — | — |
| `triage verify` | all match | mismatch | — | — |
| `doctor` | ok | not ok | — | — |

## A3

A3 means "no crash/budget atoms outside the reviewed known set".  It is
reported as `a3.status` and is **not** a claim that the legalizer is healthy.
The known set is `src/x86_mir_corpus/assets/mir-baseline-witness.json`
(C0 2062 / C1 2169 / C2 2217 / C3 2093 atoms; counts per config).

`baseline check` compares against a captured projection and reports only the
delta.  This keeps A3 findings visible without turning every run into a
failure.

## Sidecar parity gates

Before publishing a sidecar or adding an LLVM pin:

1. `x86-mir-oracle` output must be byte-identical to the in-tree filler for the
   same points file.
2. `x86-mir-run` must match `X86Tests` per atom on
   `outcome/steps/detail_code/failed_on_opcode` (stderr tails may differ in the
   program-name prefix).
3. C0–C3 counts and crash atom ids must equal `mir-baseline-witness.json`.
