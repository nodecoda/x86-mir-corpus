# Contributing

Thanks for helping.  This repository is a standalone tool: it reads LLVM
sources and links an external LLVM build, but it is **not** part of
llvm-project and must not patch or fork it.

## Development setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

The Python package has no runtime dependencies.  The C++ sidecar needs a
configured LLVM build:

```bash
x86-mir-corpus llvm build \
  --llvm-src /path/to/llvm-project \
  --llvm-build /path/to/llvm-build \
  --out .build
```

## Quality gates

Run these before sending a change:

```bash
ruff check src tests
python -m unittest discover -s tests -q
```

`python -m pytest -q` runs the same suite through pytest if you prefer it.

### Pinned-baseline parity is opt-in

`tests/test_sidecar_parity.py::test_four_config_counts_match_baseline`
compares against `src/x86_mir_corpus/assets/mir-baseline-witness.json`, which
was recorded on one specific LLVM revision.  It is skipped by default; run it
only when the sidecar is built against that revision:

```bash
X86_MIR_PARITY=1 X86_MIR_CORPUS_DIR=corpora/witness \
  python -m unittest discover -s tests -p 'test_sidecar_parity.py' -v
```

If upstream behaviour legitimately moved, refresh the witness in the same
change and explain the delta in the commit message.  Do not make the test
unconditionally green by editing the expectations.

## Test layout

- `tests/test_*.py` — unit and integration tests (`unittest` style).
- `tests/fixtures/` — small inputs checked into the repo.
- `corpora/witness/` — committed C0–C3 witness corpora.
- `corpora/full/` — generated full corpora, ignored by git.

Keep new tests deterministic: no network, no dependency on unset environment
variables, and skip (with a reason) when an optional LLVM checkout is missing.

## Evidence rules

The report contract is documented in `docs/EVIDENCE-CONTRACT.md`.  In short:

- `not_run` is never folded into success;
- infrastructure failures exit `2`;
- a run with new crashes or budgets exits `1`.

Changes to outcome names, report fields, or exit codes are breaking changes and
need a schema/version bump.

## Commit style

- One logical change per commit.
- Imperative subject line, wrapped at 72 characters.
- Reference the issue or corpus delta when the change is evidence-driven.
