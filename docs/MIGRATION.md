# Migration: replaying the `.omx` evidence

The original evidence under `llvm/trees/explore/.omx/evidence/x86-mir-corpus`
is copied to `corpora/` (see `corpora/MANIFEST.json`) and can be reproduced
with the public commands.  The four witness corpora and
their counts are the compatibility anchor:

| config | atoms | corpus |
|---|---:|---|
| C0 | 2062 | `up-C0.json` |
| C1 | 2169 | `up-C1.json` |
| C2 | 2217 | `up-C2.json` |
| C3 | 2093 | `up-C3.json` |

## Corpus

```bash
for config in C0 C1 C2 C3; do
  x86-mir-corpus corpus \
    --input src/x86_mir_corpus/assets/source-snapshots/fed20e0e-X86LegalizerInfo.cpp \
    --config "$config" --tier witness \
    --oracle=reference --oracle-binary .build/x86-mir-oracle \
    --allow-cross-revision --out "corpus/up-$config.json"
done
```

Atom bodies match the old corpora; `source` metadata and `points_sha256` differ
when the input is the packaged snapshot instead of a git revision, because the
old corpora recorded `rev: fed20e0e...` and the file path inside the checkout.

## MIR report

```bash
x86-mir-corpus mir-run --corpus corpus/up-C2.json \
  --driver .build/x86-mir-run --rules production \
  --shard-size 2000 --jobs 4 --timeout 30 --max-steps 10000 \
  --out reports/mir-M1-C2.json
```

Parity gates (`tests/test_sidecar_parity.py`) require:

- oracle output byte-identical to the in-tree filler;
- per-atom `outcome/steps/detail_code/failed_on_opcode` identical to
  `X86Tests`;
- C0–C3 counts and crash atom ids identical to
  `src/x86_mir_corpus/assets/mir-baseline-witness.json`.

## Triage

```bash
x86-mir-corpus triage cluster --report reports/mir-full-C2.json \
  --corpus corpus/upfull-C2.json --driver .build/x86-mir-run \
  --out triage/full-C2-clusters.json

x86-mir-corpus triage verify --clusters triage/full-C2-clusters.json \
  --corpus corpus/upfull-C2.json --driver .build/x86-mir-run \
  --out triage/full-C2-verified.json
```

Expected: 12 crash clusters, 192 budget clusters, `all_match: true`.

## Baseline

```bash
x86-mir-corpus baseline capture --report reports/mir-M1-C2.json \
  --out baselines/C2.json
x86-mir-corpus baseline check --report reports/mir-M1-C2.json \
  --baseline baselines/C2.json --json
```

A3 remains "not met" by design; the baseline reports only deltas against the
known set.
