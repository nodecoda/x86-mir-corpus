#!/usr/bin/env python3
"""Generate or check the legalizer DSL symbol table.

    PYTHONPATH=src python3 scripts/gen_dsl_symbols.py \
        --repo /home/ubuntu/llvm/trees/explore \
        --revision fed20e0e68323be32cb0fe8af679b8cb34e72112

Use ``--check`` in CI to fail when the pinned headers and the checked-in
``dsl_symbols.json`` disagree.
"""

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from x86_mir_corpus import dsl


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", help="llvm-project checkout")
    parser.add_argument("--llvm-src", help="llvm/ source directory")
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--out", default=dsl.default_path())
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    revision = None
    if args.repo:
        proc = subprocess.run(
            ["git", "-C", args.repo, "rev-parse", f"{args.revision}^{{commit}}"],
            capture_output=True, text=True)
        if proc.returncode != 0:
            raise SystemExit(proc.stderr.strip())
        revision = proc.stdout.strip()

    extracted = dsl.extract(llvm_src=args.llvm_src, repo=args.repo,
                            revision=args.revision)
    table = dsl.build_table(extracted, revision=revision)

    if args.check:
        current = dsl.load(args.out)
        if current == table:
            print("dsl symbols: up to date")
            return 0
        print("dsl symbols: checked-in table is stale", file=sys.stderr)
        report = dsl.verify(repo=args.repo, revision=args.revision,
                            table=current)
        for entry in report["checks"]:
            if not entry["ok"]:
                print(f"  FAIL {entry['check']}: {entry.get('detail')}",
                      file=sys.stderr)
        return 1

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(table, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(f"dsl symbols: wrote {args.out} "
          f"({table['counts']['methods']} methods, "
          f"{table['counts']['predicates']} predicates, "
          f"{table['counts']['mutations']} mutations, "
          f"{table['counts']['actions']} actions, "
          f"{table['counts']['llt']} llt constructors)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
