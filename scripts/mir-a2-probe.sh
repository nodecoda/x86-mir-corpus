#!/usr/bin/env bash
#
# A2 acceptance probe for the X86 legalizer MIR corpus runner.
#
# The merged PR #238277 fix changes the pre-TableGen C++ legalizer rules; that
# patch does not apply to this tree.  This script therefore applies a temporary
# C2 TableGen proxy: the X86_BUILD_VECTOR clampNumElements +
# moreElementsToNextPow2 sequence is replaced with clampMaxNumElements at the
# same lane limits (64/32/16/8 lanes for s8/s16/s32/s64).  This is equivalent
# to the fixed C2 rules for the probe atom and is NOT the production fix.
#
# It rebuilds X86Tests, runs the selected atom with --a2-probe (known-family
# failed/legalized is exempt from coverage_gap), then unconditionally restores
# the .td from a backup via an EXIT/INT/TERM trap, rebuilds production, and
# re-runs the A1 atom until it is a step-budget anomaly again.  The script
# exits 0 only when both sides pass and the target file is clean.
#
# Usage:
#   mir-a2-probe.sh --tree <worktree> --build <build> --corpus <up-C2.json> \
#                   --atom <id> [--timeout 120] [--max-steps 10000] \
#                   [--evidence-dir /tmp/cppdiff]
#
# Evidence JSONs are written to --evidence-dir (default /tmp/cppdiff):
#   <dir>/mir-A2-C2.json              fixed-probe report
#   <dir>/mir-A2-restore-A1-C2.json   post-restore production report

set -uo pipefail

TARGET_REL="llvm/lib/Target/X86/GISel/X86LegalizerInfo.td"
if [[ -n ${X86_MIR_CORPUS_TOOL:-} ]]; then
    corpus_tool=("$X86_MIR_CORPUS_TOOL")
elif command -v x86-mir-corpus >/dev/null 2>&1; then
    corpus_tool=(x86-mir-corpus)
else
    corpus_tool=(python3 -m x86_mir_corpus)
fi
EVIDENCE_DIR="/tmp/cppdiff"
A2_OUT="$EVIDENCE_DIR/mir-A2-C2.json"
RESTORE_A1_OUT="$EVIDENCE_DIR/mir-A2-restore-A1-C2.json"
MAX_RESTORE_ATTEMPTS=3

TREE=""
BUILD=""
CORPUS=""
ATOM=""
TIMEOUT="120"
DRIVER="${X86_MIR_RUN:-}"
MAX_STEPS="10000"

die() {
    echo "A2 FAIL: $*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage: mir-a2-probe.sh --tree <worktree> --build <build> --corpus <up-C2.json> \
                       --atom <id> [--driver <x86-mir-run>] [--timeout 120] \
                       [--max-steps 10000] [--evidence-dir <dir>]

Applies a temporary C2 TableGen proxy, rebuilds X86Tests, requires the
fixed atom to be failed|legalized, then restores and re-requires the A1
budget anomaly (steps=max_steps+1) and a clean target file.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tree)       [[ $# -ge 2 ]] || die "--tree needs a value"; TREE="$2"; shift 2 ;;
        --build)      [[ $# -ge 2 ]] || die "--build needs a value"; BUILD="$2"; shift 2 ;;
        --driver)     [[ $# -ge 2 ]] || die "--driver needs a value"; DRIVER="$2"; shift 2 ;;
        --corpus)     [[ $# -ge 2 ]] || die "--corpus needs a value"; CORPUS="$2"; shift 2 ;;
        --atom)       [[ $# -ge 2 ]] || die "--atom needs a value"; ATOM="$2"; shift 2 ;;
        --timeout)    [[ $# -ge 2 ]] || die "--timeout needs a value"; TIMEOUT="$2"; shift 2 ;;
        --max-steps)  [[ $# -ge 2 ]] || die "--max-steps needs a value"; MAX_STEPS="$2"; shift 2 ;;
        --evidence-dir)
                      [[ $# -ge 2 ]] || die "--evidence-dir needs a value"
                      EVIDENCE_DIR="$2"
                      A2_OUT="$EVIDENCE_DIR/mir-A2-C2.json"
                      RESTORE_A1_OUT="$EVIDENCE_DIR/mir-A2-restore-A1-C2.json"
                      shift 2 ;;
        -h|--help)    usage; exit 0 ;;
        *)            die "unknown argument: $1" ;;
    esac
done

[[ -n "$TREE" ]]   || die "--tree is required"
[[ -n "$BUILD" ]]  || die "--build is required"
[[ -n "$CORPUS" ]] || die "--corpus is required"
[[ -n "$ATOM" ]]   || die "--atom is required"
[[ "$TIMEOUT" =~ ^[0-9]+([.][0-9]+)?$ ]] || die "--timeout must be positive"
[[ "$MAX_STEPS" =~ ^[1-9][0-9]*$ ]] || die "--max-steps must be a positive integer"
EXPECTED_STEPS=$((MAX_STEPS + 1))

# --- Path validation: only this tree, only the X86 legalizer TableGen file.
TREE_REAL="$(cd "$TREE" 2>/dev/null && pwd -P)" || die "tree does not exist: $TREE"
TREE="$TREE_REAL"
[[ -f "$TREE/$TARGET_REL" ]] || die "missing target file: $TREE/$TARGET_REL"
TARGET="$TREE/$TARGET_REL"
TARGET_REAL="$(realpath -e "$TARGET")" || die "cannot resolve target: $TARGET"
[[ "$TARGET_REAL" == "$TREE/$TARGET_REL" ]] || \
    die "refusing target '$TARGET_REAL' outside tree '$TREE'"

BUILD_REAL="$(cd "$BUILD" 2>/dev/null && pwd -P)" || die "build does not exist: $BUILD"
[[ -f "$BUILD_REAL/build.ninja" ]] || die "not a ninja build directory: $BUILD_REAL"
BUILD="$BUILD_REAL"
[[ -n $DRIVER ]] || DRIVER="$BUILD/unittests/Target/X86/X86Tests"
[[ -x $DRIVER ]] || die "driver is not executable: $DRIVER"
CORPUS="$(realpath -e "$CORPUS")" || die "corpus does not exist"

mkdir -p "$EVIDENCE_DIR"

# The target must be clean before we patch; otherwise we could overwrite or
# silently preserve somebody else's edits in the restore path.
if ! git -C "$TREE" diff --quiet -- "$TARGET_REL"; then
    die "$TARGET_REL is already modified; refusing to patch"
fi
git -C "$TREE" ls-files --error-unmatch "$TARGET_REL" >/dev/null 2>&1 || \
    die "$TARGET_REL is not tracked by git"

BACKUP="$(mktemp "${TMPDIR:-/tmp}/x86-legalizer-info.XXXXXX.td")" || \
    die "cannot create backup"
cp -p "$TARGET" "$BACKUP" || die "cannot back up $TARGET"

PATCH_APPLIED=0
NEEDS_RESTORE=0
FINALIZE_DONE=0
FINAL_OK=0
RESTORE_OK=0
GIT_CLEAN_OK=0
A2_RC=""
A2_OUTCOME=""
A2_STEPS=""
RESTORE_RC=""
RESTORE_OUTCOME=""
RESTORE_STEPS=""

report_field() {
    local report="$1" field="$2"
    python3 - "$report" "$field" "$ATOM" <<'PY'
import json
import sys

report, field, atom = sys.argv[1:4]
with open(report, "r", encoding="utf-8") as handle:
    doc = json.load(handle)
for result in doc.get("atoms") or ():
    if result.get("id") == atom:
        print(result.get(field, ""))
        raise SystemExit(0)
raise SystemExit(1)
PY
}

rebuild_x86tests() {
    echo "A2: rebuilding X86Tests (production or probe rules)"
    ninja -C "$BUILD" -j24 X86Tests
}

run_mir_atom() {
    local out="$1"
    shift
    rm -f "$out" "$out.stdout.txt" "$out.stderr.txt"
    "${corpus_tool[@]}" mir-run \
        --corpus "$CORPUS" \
        --driver "$DRIVER" \
        --rules production \
        --timeout "$TIMEOUT" \
        --max-steps "$MAX_STEPS" \
        --only-atom "$ATOM" \
        "$@" \
        --out "$out"
}

finalize() {
    [[ $FINALIZE_DONE -eq 1 ]] && return 0

    local rc=0
    if [[ $PATCH_APPLIED -eq 1 ]]; then
        echo "A2: restoring $TARGET_REL from backup"
        # Restore through a temporary file so an interrupt cannot leave a
        # truncated tracked source.  Do not preserve the backup's old mtime:
        # ninja must see the restored source as newer than the generated .inc
        # files and rebuild production.
        local restore_tmp="$TARGET.restore.$$"
        if ! cp "$BACKUP" "$restore_tmp" || ! mv "$restore_tmp" "$TARGET" \
           || ! touch "$TARGET"; then
            echo "A2 FAIL: could not restore $TARGET_REL from $BACKUP" >&2
            GIT_CLEAN_OK=0
            return 1
        fi
        PATCH_APPLIED=0
    fi
    FINALIZE_DONE=1

    if [[ $NEEDS_RESTORE -eq 0 ]]; then
        GIT_CLEAN_OK=1
        return 0
    fi

    local attempt outcome steps
    for ((attempt = 1; attempt <= MAX_RESTORE_ATTEMPTS; attempt++)); do
        echo "A2: post-restore check attempt $attempt/$MAX_RESTORE_ATTEMPTS"
        if ! rebuild_x86tests; then
            echo "A2 FAIL: production X86Tests rebuild failed" >&2
            rc=1
            continue
        fi
        set +e
        run_mir_atom "$RESTORE_A1_OUT" \
            >"$RESTORE_A1_OUT.stdout.txt" 2>"$RESTORE_A1_OUT.stderr.txt"
        RESTORE_RC=$?
        set -e
        if [[ ! -f "$RESTORE_A1_OUT" ]]; then
            echo "A2 WARN: post-restore report missing (rc=$RESTORE_RC)" >&2
            rc=1
            continue
        fi
        outcome="$(report_field "$RESTORE_A1_OUT" outcome)" || outcome=""
        steps="$(report_field "$RESTORE_A1_OUT" steps)" || steps=""
        RESTORE_OUTCOME="$outcome"
        RESTORE_STEPS="$steps"
        echo "A2: post-restore atom outcome=${outcome:-?} steps=${steps:-?} rc=$RESTORE_RC"
        if [[ "$outcome" == "budget" && "$steps" == "$EXPECTED_STEPS" ]]; then
            RESTORE_OK=1
            rc=0
            break
        fi
        rc=1
    done

    if [[ $RESTORE_OK -ne 1 ]]; then
        echo "A2 FAIL: post-restore A1 atom is not a steps=$EXPECTED_STEPS budget anomaly" >&2
    fi
    if git -C "$TREE" diff --exit-code -- "$TARGET_REL" >/dev/null 2>&1; then
        GIT_CLEAN_OK=1
    else
        echo "A2 FAIL: $TARGET_REL is still dirty after restore" >&2
        git -C "$TREE" --no-pager diff -- "$TARGET_REL" >&2 || true
        GIT_CLEAN_OK=0
        rc=1
    fi
    return "$rc"
}

print_summary() {
    local result="FAIL"
    if [[ "$A2_OUTCOME" =~ ^(failed|legalized)$ && "$RESTORE_OUTCOME" == "budget" \
          && "$RESTORE_STEPS" == "$EXPECTED_STEPS" && $GIT_CLEAN_OK -eq 1 \
          && $FINAL_OK -eq 1 ]]; then
        result="PASS"
    fi
    echo
    echo "A2 PROBE SUMMARY"
    echo "  tree:            $TREE"
    echo "  build:           $BUILD"
    echo "  corpus:          $CORPUS"
    echo "  atom:            $ATOM"
    echo "  A2 fixed side:   outcome=${A2_OUTCOME:-?} steps=${A2_STEPS:-?} rc=${A2_RC:-?} report=$A2_OUT"
    echo "  A1 restored side: outcome=${RESTORE_OUTCOME:-?} steps=${RESTORE_STEPS:-?} rc=${RESTORE_RC:-?} report=$RESTORE_A1_OUT"
    echo "  target clean:    $([[ $GIT_CLEAN_OK -eq 1 ]] && echo yes || echo no)"
    echo "  RESULT:          $result"
}

on_exit() {
    local exit_rc=$?
    trap - EXIT INT TERM
    if [[ $FINALIZE_DONE -eq 0 ]]; then
        finalize || true
    fi
    if [[ "$A2_OUTCOME" =~ ^(failed|legalized)$ && "$RESTORE_OUTCOME" == "budget" \
          && "$RESTORE_STEPS" == "$EXPECTED_STEPS" && $GIT_CLEAN_OK -eq 1 ]]; then
        FINAL_OK=1
    else
        FINAL_OK=0
    fi
    print_summary
    if [[ $GIT_CLEAN_OK -eq 1 ]]; then
        rm -f "$BACKUP"
    else
        echo "A2: backup kept at $BACKUP" >&2
    fi
    if [[ $FINAL_OK -eq 1 ]]; then
        exit 0
    fi
    if [[ $exit_rc -eq 0 ]]; then
        exit 1
    fi
    exit "$exit_rc"
}

trap on_exit EXIT
trap 'echo "A2: interrupted by SIGINT" >&2; exit 130' INT
trap 'echo "A2: interrupted by SIGTERM" >&2; exit 143' TERM

# --- Install the temporary C2 proxy patch.
echo "A2: applying temporary C2 TableGen proxy to $TARGET_REL"
python3 - "$TARGET" <<'PY'
import os
import re
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as handle:
    text = handle.read()

block_re = re.compile(
    r'(?ms)^def X86_BUILD_VECTOR\s*:\s*LegalizeRuleSet<\["G_BUILD_VECTOR"\]>\s*\{.*?^\}')
blocks = list(block_re.finditer(text))
if len(blocks) != 1:
    sys.exit(f"A2 FAIL: expected exactly one X86_BUILD_VECTOR rule set, found {len(blocks)}")
block = blocks[0]

old = (
    "    ClampNumElements<0, X86_v16s8, X86_s8MaxVector>,\n"
    "    ClampNumElements<0, X86_v8s16, X86_s16MaxVector>,\n"
    "    ClampNumElements<0, X86_v4s32, X86_s32MaxVector>,\n"
    "    ClampNumElements<0, X86_v2s64, X86_s64MaxVector>,\n"
    "    MoreElementsToNextPow2<0>,\n"
)
new = (
    "    ClampMaxNumElements<0, X86_s8, 64>,\n"
    "    ClampMaxNumElements<0, X86_s16, 32>,\n"
    "    ClampMaxNumElements<0, X86_s32, 16>,\n"
    "    ClampMaxNumElements<0, X86_s64, 8>,\n"
)
body = block.group(0)
count = body.count(old)
if count != 1:
    sys.exit(f"A2 FAIL: expected exactly one clampNumElements+moreElements "
             f"fragment in X86_BUILD_VECTOR, found {count}")
patched_body = body.replace(old, new, 1)
patched = text[:block.start()] + patched_body + text[block.end():]

line = text[:block.start()].count("\n") + 1
print(f"A2: X86_BUILD_VECTOR rule set at line {line}; replaced 5 lines with 4")

tmp = path + ".a2-probe.tmp"
with open(tmp, "w", encoding="utf-8") as handle:
    handle.write(patched)
os.chmod(tmp, os.stat(path).st_mode)
os.replace(tmp, path)
PY
PATCH_APPLIED=1
NEEDS_RESTORE=1

# --- Fixed side: rebuild and run with --a2-probe.
if ! rebuild_x86tests; then
    die "fixed-rules X86Tests rebuild failed"
fi

echo "A2: running fixed-rules probe atom"
set +e
run_mir_atom "$A2_OUT" --a2-probe \
    >"$A2_OUT.stdout.txt" 2>"$A2_OUT.stderr.txt"
A2_RC=$?
set -e
[[ -f "$A2_OUT" ]] || die "fixed-rules report was not written: $A2_OUT"
A2_OUTCOME="$(report_field "$A2_OUT" outcome)" || A2_OUTCOME=""
A2_STEPS="$(report_field "$A2_OUT" steps)" || A2_STEPS=""
echo "A2: fixed atom outcome=${A2_OUTCOME:-?} steps=${A2_STEPS:-?} rc=$A2_RC"
if [[ ! "$A2_OUTCOME" =~ ^(failed|legalized)$ ]]; then
    die "fixed-rules atom ended '${A2_OUTCOME:-missing}' (expected failed|legalized)"
fi

# --- Restore, rebuild production, and re-assert A1.
if ! finalize; then
    die "post-restore A1 re-assertion failed"
fi
FINAL_OK=1
exit 0
