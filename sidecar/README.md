# C++ sidecar

Two out-of-tree executables built against an LLVM install/build:

- `x86-mir-oracle` (`oracle/CorpusOracle.cpp`) answers legality points with
  the frozen `X86LegalizerInfoReference`.
- `x86-mir-run` (`mir/MIRRunner.cpp`) builds a `MachineFunction` per atom,
  verifies it, and runs `legalizeMachineFunction`.
- `x86-mir-smoke` is a one-query build/link smoke test.

## Build

```bash
cmake -S sidecar -B .build -G Ninja \
  -DLLVM_DIR=$X86_MIR_LLVM_BUILD/lib/cmake/llvm
cmake --build .build --target x86-mir-oracle x86-mir-run
```

`X86_SOURCE_DIR` defaults to `LLVM_BUILD_MAIN_SRC_DIR/lib/Target/X86` and can
be overridden for a relocated source tree.  The configure step writes
`sidecar-build-info.json`, which `x86-mir-corpus doctor` uses to check that
the sidecar and the LLVM build agree.

## Assertions/ABI hazard

`llvm::LegalizeRuleSet` has assert-only members.  A client compiled with
`-DNDEBUG` therefore has a **different layout** than an assertions-enabled
`libLLVM`, and the base `LegalizerInfo` array is indexed with the wrong
stride.  The symptom is a bogus `Opcode is already aliased to another opcode`
assertion.  CMake forces `-UNDEBUG` when `LLVM_ENABLE_ASSERTIONS` is ON; keep
that behavior.

## Parity

`tests/test_sidecar_parity.py` compares, when the build and cached corpora are
available:

- oracle output byte-for-byte against the legacy in-tree filler;
- MIR per-atom `outcome/steps/detail_code/failed_on_opcode` against `X86Tests`;
- C0–C3 counts and crash atom ids against `mir-baseline-witness.json`.
