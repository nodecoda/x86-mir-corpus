# Documentation index

| Document | What it covers |
|---|---|
| [USAGE.md](USAGE.md) | Commands, environments, and worked examples |
| [PIPELINE.md](PIPELINE.md) | Parse → points → corpus → run → triage flow |
| [DSL-SURFACE.md](DSL-SURFACE.md) | Symbol table, scope audit, and verifier invariants |
| [QUOTIENT.md](QUOTIENT.md) | Finite quotient: keys, axes, lengths, panel coverage |
| [TRIAGE.md](TRIAGE.md) | Action → `LegalizerHelper` entry-point mapping |
| [EVIDENCE-CONTRACT.md](EVIDENCE-CONTRACT.md) | Outcome semantics, exit codes, report schema |
| [LLVM-INTEGRATION.md](LLVM-INTEGRATION.md) | Building and pinning the C++ sidecar |
| [ACCEPTANCE.md](ACCEPTANCE.md) | v0.1.0 acceptance evidence |
| [MIGRATION.md](MIGRATION.md) | Corpus schema migration (v1 → v2) |
| [PRODUCTIZATION.md](PRODUCTIZATION.md) | Quality bar, test determinism, baseline refresh |
| [ANALYSIS-FAILED.md](ANALYSIS-FAILED.md) | Full-corpus `failed` analysis (2026-11-04) |
| [ANALYSIS-CRASH.md](ANALYSIS-CRASH.md) | Full-corpus `crash` analysis: 12 assertion families (2026-11-04) |
| [ANALYSIS-AARCH64-CRASH.md](ANALYSIS-AARCH64-CRASH.md) | AArch64 A2 crash triage and role-aware type contracts (2026-11-05) |
| [UPSTREAM-README.md](UPSTREAM-README.md) | Upstream-facing description of the tool |

Start with `USAGE.md`; the evidence contract is the reference for anything
that consumes a report.
