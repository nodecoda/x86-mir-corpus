# Legalizer DSL surface

The parser is driven by one checked-in symbol table,
`src/x86_mir_corpus/assets/dsl_symbols.json`. It is generated from a pinned
LLVM revision, not maintained by hand:

| Section | Source of truth | Used for |
|---|---|---|
| `methods` | `LegalizerInfo.h` | chain methods and their positional `roles` |
| `predicates` | `LegalizerInfo.h` | `LegalityPredicates` and combinators |
| `mutations` | `LegalizerInfo.h` | `LegalizeMutations` and their argument roles |
| `llt_constructors`, `llt_queries` | `LowLevelType.h` | LLT construction and queries |
| `element_count` | `TypeSize.h` | `ElementCount` and `TypeSize` helpers |
| `query_api` | `LegalizerInfo.h` | `LegalityQuery` fields |
| `legalizer_info_api` | `LegalizerInfo.h` | constructor/runtime entry points |
| `helper_entrypoints` | `LegalizerHelper.h` | triage mapping (see `TRIAGE.md`) |
| `opcodes` | `GenericOpcodes.td` | dense `num_types` / `num_imms` per `G_*` |
| `enums`, `namespaces`, `ignore`, `target_functions`, `target_queries` | headers + target sources | audit classification |

Every entry carries `source` (`file:line`), `resolution`
(`exact` / `opaque` / `ignore`) and, where positional, `roles`
(`slot`, `predicate`, `mutation`, `llt`, `size`, `type_list`, ...).

## Three-way consistency

1. **Extraction** (`dsl.extract`) reads the pinned files.
2. **Table** (`dsl_symbols.json`) is the curated surface.
3. **Runtime** (`expr.py`, `parse.py`, `domain.py`) reads the table.

`tests/test_dsl_dispatch.py` asserts that the runtime registries and the table's
`exact` sets are equal in both directions. A name added to one side without the
other fails the suite.

## Scope model

`dsl audit` partitions the target source structurally:

- `constructor`: the body of `<Target>LegalizerInfo::<Target>LegalizerInfo`.
- `lambda`: a lambda body nested in the constructor.
- `lowering`: every other function body, including namespace-scope helpers.
- `file`: declarations, namespaces and includes.

Only `constructor` and `lambda` are gated. The lowering surface is reported
separately because it uses the full LLVM API and is not parser input.

```bash
x86-mir-corpus dsl verify --repo /path/to/llvm-project \
  --revision fed20e0e68323be32cb0fe8af679b8cb34e72112
x86-mir-corpus dsl audit --target aarch64 --repo /path/to/llvm-project --strict
x86-mir-corpus dsl audit --target x86 --input path/to/X86LegalizerInfo.cpp --strict
```

`--strict` fails when a gated scope has an unknown call or when the
`surface_sha256` differs from `assets/targets/<target>.surface.json`. Intentional
surface changes require `--update-manifest`, so the diff is reviewed.

## Corresponding LLVM verifier logic

| LLVM pin location | Check | Python counterpart |
|---|---|---|
| `LegalizerInfo.cpp:116` `mutationIsSane` | mutation slot/type validity | `domain.construction_issues` (`mutation_type_idx`, `mutation_source_slot`) |
| `LegalizerInfo.cpp:191` `LegalizeRuleSet::apply` | first matching rule wins, null mutation is skipped | `domain.first_match`, `parse.extract_mutation` (`kind: none`) |
| `LegalizerInfo.cpp:211` `verifyTypeIdxsCoverage` | dense type indices covered | `shape_arity` (opcode arity vs rule shape) |
| `LegalizerInfo.cpp:242` `verifyImmIdxsCoverage` | dense immediate indices covered | `imm_idx`, `imm_coverage` |
| `LegalizerInfo.cpp:308` `getActionDefinitionsBuilder` | builder boundaries | `audit` constructor region |

`domain.build_points` adds a `coverage.construction` section with the issue
list and per-code counts. These issues do not drop points; they make shape gaps
explicit. A `shape_arity` issue means the rule set constrains fewer type
indices than the opcode declares, so the generated corpus for that builder does
not vary the missing slots.

## Pin bump flow

```bash
x86-mir-corpus pin update --target aarch64 --repo /path/to/llvm-project <rev>
PYTHONPATH=src python3 scripts/gen_dsl_symbols.py \
  --repo /path/to/llvm-project --revision <rev>
x86-mir-corpus dsl verify --repo /path/to/llvm-project --revision <rev>
x86-mir-corpus dsl audit --target x86 --repo ... --strict --update-manifest
x86-mir-corpus dsl audit --target aarch64 --repo ... --strict --update-manifest
```

The manifest diff is the review surface: new `exact`/`opaque`/`unknown` names
must have a reason before the pin moves.
