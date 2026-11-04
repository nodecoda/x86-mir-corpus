# Bundled corpora

Copied from `.omx/evidence/x86-mir-corpus` (llvm-project `8bc4cd96f4bb`).
`MANIFEST.json` records SHA-256, atom counts, and counts per config.

```
corpora/
  witness/   up-C{0..3}.json, .points.json, .report.json   committed (~3.9 MB)
  reports/   mir-M1-C{0..3}.json                            committed (~3 MB)
  triage/    full-C2 and witness cluster/verification       committed (~0.3 MB)
  full/      upfull-C2.json (51 MB) + mir-full-C2.json (85 MB)
             gitignored; present in a working checkout, not in git clones
```

## Use directly

Parity tests default to `corpora/witness` when `X86_MIR_CORPUS_DIR` is unset:

```bash
X86_MIR_LLVM_SRC=... X86_MIR_LLVM_BUILD=... \
X86_MIR_SIDECAR_BUILD=./.build \
python3 -m unittest discover -s tests -p 'test_sidecar_parity.py' -v
```

Triage replay:

```bash
x86-mir-corpus triage cluster \
  --report corpora/full/mir-full-C2.json \
  --corpus corpora/full/upfull-C2.json \
  --driver .build/x86-mir-run --out /tmp/clusters.json
```

Expected: 12 crash clusters, 192 budget clusters.

MIR replay:

```bash
x86-mir-corpus mir-run --corpus corpora/witness/up-C2.json \
  --driver .build/x86-mir-run --out /tmp/report.json
```

The `reports/mir-M1-*.json` files are the source reports for
`mir-baseline-witness.json`.  They predate the `failed_on_opcode` field; the
current sidecar adds it.  Counts and atom ids are unchanged.
