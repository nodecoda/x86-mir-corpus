#!/usr/bin/env python3
"""Emit the finite-quotient report for one legalizer constructor.

    PYTHONPATH=src python3 scripts/quotient_report.py \
        --target aarch64 --config A2 \
        --repo /home/ubuntu/llvm/llvm-project \
        --out /tmp/quotient-a64-A2.json

The report is read-only: it never changes panels, corpora, or the pinned
revisions.  Use it to compare a panel against the quotient classes and to
decide which axes need more representatives.
"""

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from x86_mir_corpus import parse, pin, quotient, targets


def load_text(repo, revision, path):
    if revision is None:
        with open(path, encoding="utf-8") as handle:
            return handle.read(), None
    proc = subprocess.run(
        ["git", "-C", repo, "show", f"{revision}:{path}"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.strip() or f"git show failed: {path}")
    return proc.stdout, revision


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="x86", choices=targets.names())
    parser.add_argument("--config")
    parser.add_argument("--repo", default="/home/ubuntu/llvm/llvm-project")
    parser.add_argument("--source-path")
    parser.add_argument("--source-rev")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    profile = targets.get(args.target)
    config = args.config or profile.default_config
    if config not in profile.configs:
        raise SystemExit(f"unknown config {config!r} for {args.target}")
    source_path = args.source_path or profile.source_path
    revision = args.source_rev
    if revision is None:
        revision = pin.revision(target=args.target)
    text, revision = load_text(args.repo, revision, source_path)
    result = parse.parse_source(
        text, {"rev": revision, "path": source_path}, config,
        profile.configs[config], profile.rule_set_prefix, profile.llt_extended)
    report = quotient.analyze(result, profile, config)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=1, sort_keys=True)
        handle.write("\n")
    summary = {
        "out": os.path.abspath(args.out),
        "target": args.target,
        "config": config,
        "axes": {name: {"keys": axis["keys"],
                        "length": axis["length"],
                        "unrepresented": axis["unrepresented"]}
                 for name, axis in report["axes"].items()},
        "coverage": report["coverage"],
        "opaque_builders": len(report["opaque_builders"]),
    }
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
