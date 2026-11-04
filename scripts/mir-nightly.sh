#!/usr/bin/env bash
# Nightly full-corpus run for the X86 legalizer MIR supervisor.
#
# For each config this runs every atom through the real legalizer with a
# per-shard timeout and the production rule set.  The supervisor's exit 1 is
# the expected anomaly evidence (budget/hang/crash); exit 2, a missing report,
# or a report whose counts do not conserve corpus.atoms is an infrastructure
# failure.  A compact summary.json is written under --out-dir.
#
# Usage: mir-nightly.sh [--build <build>] [--corpus-dir <dir>]
#                       [--out-dir <dir>] [--jobs N] [--timeout S]
#                       [--max-steps N] [--configs C0,C1,C2,C3]
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: mir-nightly.sh [--build <build>] [--corpus-dir <dir>]
                      [--out-dir <dir>] [--jobs N] [--timeout S]
                      [--max-steps N] [--configs C0,C1,C2,C3]

Run the X86 legalizer MIR corpus supervisor over the full corpora.

Options:
  --build <dir>       LLVM build directory containing X86Tests
                      (default: $LLVM_BUILD when set; required otherwise)
  --corpus-dir <dir>  directory holding upfull-<C>.json or up-<C>.json
                      (required)
  --out-dir <dir>     artifact directory
                      (default: /tmp/x86-mir-nightly/<UTC timestamp>)
  --jobs N            concurrent shards (default: 8; each driver loads the corpus)
  --timeout S         per-shard timeout in seconds (default: 30)
  --max-steps N       legalizer step budget (default: 10000)
  --configs LIST      comma-separated configs (default: C0,C1,C2,C3)
  --dry-run           print the resolved plan and exit without running

For each config the input is <corpus-dir>/upfull-<C>.json, falling back to
<corpus-dir>/up-<C>.json when the full corpus is absent (reported explicitly).
A config succeeds only when mir-run exits 0 or 1, writes its report, and
sum(counts) == corpus.atoms.  summary.json records the per-config counts,
first anomaly, A3 status, known/new anomaly counts, and artifact paths.
EOF
}

die() {
    echo "mir-nightly: $*" >&2
    exit 2
}

tool_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if command -v x86-mir-corpus >/dev/null 2>&1; then
    tool=(x86-mir-corpus)
else
    tool=(python3 -m x86_mir_corpus)
fi

build=${LLVM_BUILD:-}
driver=${X86_MIR_RUN:-}
corpus_dir=
out_dir=
jobs=8
timeout=30
max_steps=10000
configs_arg=C0,C1,C2,C3
dry_run=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --build)
            [[ $# -ge 2 ]] || die "missing value for --build"
            build=$2
            shift 2
            ;;
        --driver)
            [[ $# -ge 2 ]] || die "missing value for --driver"
            driver=$2
            shift 2
            ;;
        --corpus-dir)
            [[ $# -ge 2 ]] || die "missing value for --corpus-dir"
            corpus_dir=$2
            shift 2
            ;;
        --out-dir)
            [[ $# -ge 2 ]] || die "missing value for --out-dir"
            out_dir=$2
            shift 2
            ;;
        --jobs)
            [[ $# -ge 2 ]] || die "missing value for --jobs"
            jobs=$2
            shift 2
            ;;
        --timeout)
            [[ $# -ge 2 ]] || die "missing value for --timeout"
            timeout=$2
            shift 2
            ;;
        --max-steps)
            [[ $# -ge 2 ]] || die "missing value for --max-steps"
            max_steps=$2
            shift 2
            ;;
        --configs)
            [[ $# -ge 2 ]] || die "missing value for --configs"
            configs_arg=$2
            shift 2
            ;;
        --dry-run)
            dry_run=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown argument: $1"
            ;;
    esac
done

if [[ -z $driver && -z $build ]]; then
    for candidate in "$tool_dir/../.build/x86-mir-run" \
                     "$(command -v x86-mir-run 2>/dev/null || true)"; do
        if [[ -n $candidate && -x $candidate ]]; then driver=$candidate; break; fi
    done
fi
[[ -n $driver || -n $build ]] || die "pass --driver/--build or set X86_MIR_RUN"
[[ -n $corpus_dir ]] || die "--corpus-dir must not be empty"
[[ -n $configs_arg ]] || die "--configs must not be empty"

if ! [[ $jobs =~ ^[1-9][0-9]*$ ]]; then
    die "--jobs must be a positive integer"
fi
if ! [[ $max_steps =~ ^[1-9][0-9]*$ ]]; then
    die "--max-steps must be a positive integer"
fi
if ! [[ $timeout =~ ^[0-9]+([.][0-9]+)?$ ]] ||
   ! awk -v t="$timeout" 'BEGIN { exit !(t > 0) }'; then
    die "--timeout must be a positive number"
fi

IFS=',' read -r -a config_list <<< "$configs_arg"
[[ ${#config_list[@]} -gt 0 ]] || die "--configs must not be empty"
seen_configs=","
for config in "${config_list[@]}"; do
    [[ -n $config ]] || die "empty config in --configs"
    [[ $config =~ ^C[0-3]$ ]] ||
        die "unsupported config '$config' (expected C0-C3)"
    case ",$seen_configs," in
        *",$config,"*) die "duplicate config '$config'" ;;
    esac
    seen_configs="$seen_configs$config,"
done

if [[ -z $out_dir ]]; then
    out_dir="${TMPDIR:-/tmp}/x86-mir-nightly/$(date -u +%Y%m%dT%H%M%SZ)"
fi

if [[ $dry_run -eq 1 ]]; then
    echo "mir-nightly: dry run"
    echo "  build:      $build"
    echo "  corpus-dir: $corpus_dir"
    echo "  out-dir:    $out_dir"
    echo "  jobs:       $jobs"
    echo "  timeout:    $timeout"
    echo "  max-steps:  $max_steps"
    echo "  configs:    $configs_arg"
    for config in "${config_list[@]}"; do
        if [[ -f "$corpus_dir/upfull-$config.json" ]]; then
            echo "  $config: upfull-$config.json"
        elif [[ -f "$corpus_dir/up-$config.json" ]]; then
            echo "  $config: up-$config.json (fallback; upfull absent)"
        else
            echo "  $config: missing (upfull-$config.json and up-$config.json)"
        fi
    done
    exit 0
fi

mkdir -p "$out_dir"
out_dir=$(cd "$out_dir" && pwd)
parts_dir=$(mktemp -d "$out_dir/.mir-nightly-parts.XXXXXX")

# Write one compact per-config record.  The record is always written so a
# failed config cannot leave a stale success from a previous run.
write_config_summary() {
    local config=$1
    local corpus=$2
    local source=$3
    local report=$4
    local run_rc=$5
    local part=$6
    local pre_error=${7:-}
    python3 - "$config" "$corpus" "$source" "$report" "$run_rc" "$part" \
        "$pre_error" <<'PY'
import json
import os
import sys

config, corpus, source, report, run_rc_text, part, pre_error = sys.argv[1:8]
run_rc = int(run_rc_text)
entry = {
    "config": config,
    "corpus": corpus,
    "corpus_source": source,
    "report": report,
    "exit_code": run_rc,
    "ok": False,
}
try:
    if pre_error:
        entry["error"] = pre_error
    elif run_rc not in (0, 1):
        entry["error"] = f"mir-run exited {run_rc}; expected 0 or 1"
    elif not os.path.isfile(report):
        entry["error"] = "mir-run did not write a report"
    else:
        with open(report, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
        corpus_info = doc.get("corpus") or {}
        atoms = corpus_info.get("atoms")
        if not isinstance(atoms, int) or atoms < 0:
            raise ValueError("report has no non-negative corpus.atoms")
        counts = doc.get("counts") or {}
        total = 0
        for name, value in counts.items():
            if not isinstance(value, int):
                raise ValueError(f"report count {name!r} is not an integer")
            total += value
        if total != atoms:
            raise ValueError(
                f"sum(counts)={total} != corpus.atoms={atoms}")
        a3 = doc.get("a3") or {}
        first = doc.get("first_anomaly")
        entry.update({
            "atoms": atoms,
            "counts": counts,
            "counts_sum": total,
            "first_anomaly": None if first is None else first.get("id"),
            "a3_status": a3.get("status"),
            "known_count": a3.get("known_anomalies", 0),
            "new_count": a3.get("new_findings", 0),
            "coverage_gaps": a3.get("coverage_gaps", 0),
            "ok": True,
        })
except Exception as exc:  # Report every failure instead of dropping a config.
    entry["error"] = f"invalid report: {exc}"

with open(part, "w", encoding="utf-8") as handle:
    json.dump(entry, handle, indent=1, sort_keys=True)
    handle.write("\n")

if not entry["ok"] and not pre_error:
    print(f"mir-nightly: {config}: {entry.get('error', 'unknown error')}",
          file=sys.stderr)
print(json.dumps(entry, sort_keys=True))
raise SystemExit(0 if entry["ok"] else 1)
PY
}

for config in "${config_list[@]}"; do
    full="$corpus_dir/upfull-$config.json"
    fallback="$corpus_dir/up-$config.json"
    report="$out_dir/mir-full-$config.json"

    if [[ -f $full ]]; then
        corpus=$full
        corpus_source=upfull
    elif [[ -f $fallback ]]; then
        corpus=$fallback
        corpus_source=up-fallback
        echo "mir-nightly: $config: upfull-$config.json absent; using fallback up-$config.json" >&2
    else
        echo "mir-nightly: $config: neither $full nor $fallback exists" >&2
        if ! write_config_summary "$config" "$full" missing "$report" 2 \
                "$parts_dir/$config.json" \
                "corpus not found: neither $full nor $fallback exists" \
                >/dev/null; then
            :
        fi
        continue
    fi

    unlink "$report" 2>/dev/null || true
    echo "mir-nightly: $config: $corpus ($corpus_source)"

    rc=0
    run_args=(mir-run --corpus "$corpus" --rules production)
    if [[ -n $driver ]]; then
        run_args+=(--driver "$driver")
    else
        run_args+=(--build "$build")
    fi
    "${tool[@]}" "${run_args[@]}" \
        --shard-size 2000 \
        --jobs "$jobs" \
        --timeout "$timeout" \
        --max-steps "$max_steps" \
        --out "$report" || rc=$?

    if ! write_config_summary "$config" "$corpus" "$corpus_source" \
            "$report" "$rc" "$parts_dir/$config.json" >/dev/null; then
        :
    fi
done

python3 - "$out_dir/summary.json" "$out_dir" "$parts_dir" "$build" \
    "$corpus_dir" "$jobs" "$timeout" "$max_steps" "$configs_arg" <<'PY'
import json
import os
import sys

(summary_path, out_dir, parts_dir, build, corpus_dir,
 jobs, timeout, max_steps, configs_arg) = sys.argv[1:10]

configs = [config for config in configs_arg.split(",") if config]
entries = {}
failures = []
for config in configs:
    part = os.path.join(parts_dir, config + ".json")
    if os.path.isfile(part):
        with open(part, "r", encoding="utf-8") as handle:
            entry = json.load(handle)
    else:
        entry = {
            "config": config,
            "ok": False,
            "error": "config summary was not produced",
        }
    entries[config] = entry
    if not entry.get("ok"):
        failures.append({
            "config": config,
            "error": entry.get("error", "unknown error"),
        })

summary = {
    "schema": "llvm-x86-legalizer-mir-nightly",
    "version": 1,
    "build": build,
    "corpus_dir": corpus_dir,
    "out_dir": out_dir,
    "rules": "production",
    "jobs": int(jobs),
    "timeout_s": float(timeout),
    "max_steps": int(max_steps),
    "ok": not failures,
    "configs": entries,
    "failures": failures,
}
with open(summary_path, "w", encoding="utf-8") as handle:
    json.dump(summary, handle, indent=1, sort_keys=True)
    handle.write("\n")


def fmt_counts(counts):
    order = ("legalized", "failed", "budget", "hang", "crash",
             "unbuildable", "construction_mismatch", "not_run")
    parts = [f"{name}={counts[name]}" for name in order if name in counts]
    parts.extend(f"{name}={counts[name]}" for name in sorted(counts)
                 if name not in order)
    return " ".join(parts)


print()
print("{:<6} {:<12} {:>7} {:<3} {:<7} {:>5} {:>4} {:>4} {:>3} {}".format(
    "CONFIG", "SOURCE", "ATOMS", "OK", "A3", "KNOWN", "NEW", "GAPS", "RC",
    "COUNTS"))
for config in configs:
    entry = entries[config]
    if not entry.get("ok"):
        print("{:<6} {:<12} {:>7} {:<3} {}".format(
            config, entry.get("corpus_source", "-"), "-", "no",
            entry.get("error", "unknown error")))
        continue
    print("{:<6} {:<12} {:>7} {:<3} {:<7} {:>5} {:>4} {:>4} {:>3} {}".format(
        config,
        entry.get("corpus_source", "-"),
        entry.get("atoms", "-"),
        "yes",
        entry.get("a3_status") or "-",
        entry.get("known_count", 0),
        entry.get("new_count", 0),
        entry.get("coverage_gaps", 0),
        entry.get("exit_code", "-"),
        fmt_counts(entry.get("counts") or {}),
    ))
    print("       corpus: {}".format(entry.get("corpus", "-")))
    print("       report: {}".format(entry.get("report", "-")))
    print("       first_anomaly: {}".format(
        entry.get("first_anomaly") or "-"))

print()
print("summary: {}".format(summary_path))
if failures:
    print("FAILED: {} config(s) had infrastructure or count errors".format(
        len(failures)), file=sys.stderr)
    for failure in failures:
        print("  {}: {}".format(failure["config"], failure["error"]),
              file=sys.stderr)
    raise SystemExit(1)
raise SystemExit(0)
PY
