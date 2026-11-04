#!/usr/bin/env bash
# Replay one corpus through both oracles with the key -> result trace driver,
# then compare the two traces.  Exits non-zero on any difference.
#
# Usage: run-traces.sh CORPUS [OUT_DIR] [BUILD_DIR]
#
# BUILD_DIR must contain unittests/Target/X86/X86Tests (the same build the
# corpus was generated with).  It defaults to $LLVM_BUILD.
set -euo pipefail

if command -v x86-mir-corpus >/dev/null 2>&1; then
  tool=(x86-mir-corpus)
else
  tool=(python3 -m x86_mir_corpus)
fi
corpus=${1:?usage: run-traces.sh CORPUS [OUT_DIR] [BUILD_DIR]}
out=${2:-$(mktemp -d)}
build=${3:-${LLVM_BUILD:-}}
tests="$build/unittests/Target/X86/X86Tests"

if [[ -z "$build" || ! -x "$tests" ]]; then
  echo "need a build dir with unittests/Target/X86/X86Tests" >&2
  echo "usage: run-traces.sh CORPUS [OUT_DIR] BUILD_DIR" >&2
  exit 2
fi
mkdir -p "$out"
name=$(basename "$corpus" .json)

run_driver() {
  local filter=$1 dest=$2
  X86_LEGALIZER_TRACE_CORPUS_IN="$corpus" \
  X86_LEGALIZER_TRACE_CORPUS_OUT="$dest" \
    "$tests" --gtest_also_run_disabled_tests \
      --gtest_filter="$filter"
}

run_driver '*TraceCorpusWithReference*' "$out/trace-reference-$name.json"
run_driver '*TraceCorpusWithTable*' "$out/trace-table-$name.json"

"${tool[@]}" trace-diff \
    --left "$out/trace-reference-$name.json" \
    --right "$out/trace-table-$name.json" \
    --out "$out/trace-diff-$name.json" --strict

echo "trace artifacts: $out"
