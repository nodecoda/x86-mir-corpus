#!/usr/bin/env bash
# Nightly drift check for the hand-written X86 legalizer chain.
#
# The only failing condition is a source the reader cannot parse or a coverage
# gap that has no reviewed exception.  Query-domain differences between
# upstream/main and the frozen reference are expected (today: the CTPOP
# delta) and are reported, not failed.
#
# Usage: upstream-nightly.sh [ARTIFACT_DIR]
set -euo pipefail

if command -v x86-mir-corpus >/dev/null 2>&1; then
  tool=(x86-mir-corpus)
else
  tool=(python3 -m x86_mir_corpus)
fi
repo=${X86_MIR_LLVM_SRC:-}
if [[ -z $repo ]]; then
  echo "set X86_MIR_LLVM_SRC to an llvm-project checkout" >&2
  exit 2
fi
out=${1:-$(mktemp -d)}
mkdir -p "$out"

"${tool[@]}" parse --repo "$repo" --source-rev upstream/main \
    --out "$out/upstream-ast.json"
"${tool[@]}" points --repo "$repo" --source-rev upstream/main \
    --tier witness --out "$out/upstream-points.json"
"${tool[@]}" corpus --repo "$repo" --source-rev upstream/main \
    --oracle=none --tier witness --out "$out/upstream-shapes.json"

witness="$repo/llvm/unittests/Target/X86/x86-legalizer-witness.json"
if [[ -f "$witness" ]]; then
  # Informational: a shapes-only corpus has no expected answers, and a
  # cross-revision domain difference is the point of the check.
  "${tool[@]}" diff --left "$out/upstream-shapes.json" \
      --right "$witness" --out "$out/upstream-vs-witness.json"
fi

echo "nightly artifacts: $out"
