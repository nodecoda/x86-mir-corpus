# LLVM integration

The sidecar is built **out of tree** against an existing LLVM build.  No file
in the LLVM checkout is modified.

## Pins

- LLVM source/build: `8bc4cd96f4bb` (original) and `6706952f4a5a`
  (validated with the contract-aware parity gate; assertions ON, X86 and
  AArch64 targets).
- Frozen reference: `ebac17721d12`.
- Supported pins: `sidecar/supported-revisions.json`.  `doctor` rejects other
  revisions unless `--allow-unpinned` is passed.

## Required CMake inputs

```bash
cmake -S sidecar -B .build -G Ninja \
  -DLLVM_DIR=$X86_MIR_LLVM_BUILD/lib/cmake/llvm \
  -DCMAKE_BUILD_TYPE=Release
cmake --build .build --target x86-mir-oracle x86-mir-run
```

`sidecar/CMakeLists.txt` resolves:

- LLVM headers from `LLVM_INCLUDE_DIRS`;
- generated X86 headers from `$LLVM_BINARY_DIR/lib/Target/X86`;
- X86 target source headers from `LLVM_BUILD_MAIN_SRC_DIR/lib/Target/X86`
  (override with `-DX86_SOURCE_DIR=...`);
- components `X86CodeGen X86Desc X86Info GlobalISel CodeGen CodeGenTypes MC
  MIRParser Support TargetParser Analysis Core`.

The configure step writes `sidecar-build-info.json`; `doctor` compares its
LLVM version/source directory with the selected build.

## Assertions and ABI

`llvm::LegalizeRuleSet` is larger when assertions are enabled (`TypeIdxsCovered`
and `ImmIdxsCovered`).  A client built with `-DNDEBUG` therefore disagrees with
an assertions-enabled `libLLVM` about the stride of `RulesForOpcode`, producing
spurious `Opcode is already aliased to another opcode` assertions or corrupted
rule lookups.

`sidecar/CMakeLists.txt` appends `-UNDEBUG` whenever
`LLVM_ENABLE_ASSERTIONS` is ON.  Do not remove it.  If you build the sidecar
against a release LLVM without assertions, both sides must be consistent and
`doctor` will report `LLVM build has assertions disabled`.

## Legacy mode

`mir-run --build <dir>` and `corpus --build <dir>` still use the in-tree
`X86Tests` driver.  This keeps the trace drivers and old evidence reproducible
while the sidecar is validated by `tests/test_sidecar_parity.py`.

## Upgrade procedure

1. Build LLVM at the new revision with assertions and X86.
2. Rebuild the sidecar; run `tests/test_sidecar_parity.py` with cached corpora.
3. Require oracle byte equality and MIR per-atom equality (on atoms the
   sidecar hands to the legalizer; construction-rejected atoms are exempt)
   before adding the revision to `sidecar/supported-revisions.json`.
4. Regenerate baselines with `baseline capture` and record the change.
