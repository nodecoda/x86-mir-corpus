"""Oracle adapters for the X86 legalizer corpus tool.

Query shapes come from the parsed C++ source.  Expected actions come only
from an oracle, and the oracle revision is recorded next to the source
revision.  The Python side never invents an expected action.
"""

import json
import os
import shutil
import subprocess

REFERENCE_PROVENANCE = os.path.join(
    "llvm", "unittests", "Target", "X86",
    "x86-legalizer-reference-provenance.json")
BUNDLED_PROVENANCE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "assets",
    "x86-legalizer-reference-provenance.json")


def reference_revision(repo="."):
    """The frozen C++ reference commit, or None when provenance is missing.

    An llvm-project checkout keeps the provenance next to the in-tree tests.
    Standalone use falls back to the copy bundled with this tool.
    """
    for path in (os.path.join(repo, REFERENCE_PROVENANCE), BUNDLED_PROVENANCE):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        return data.get("source_commit")
    return None


def oracle_doc(kind, revision=None, cross_revision=False, spec=None):
    doc = {
        "kind": kind,
        "revision": revision,
        "cross_revision": bool(cross_revision),
    }
    if spec is not None and spec != revision:
        doc["spec"] = spec
    return doc


def _rule_keys(result):
    out = {}
    for builder in result.builders:
        for rule in builder.rules:
            if rule.active is False:
                continue
            key = (builder.name, rule.method,
                   rule.arg_text[0] if rule.arg_text else "")
            out.setdefault(key, []).append(rule.index)
    return out


def rule_delta(source, oracle):
    """Rules that exist on one side only, keyed by text, not by index."""
    left, right = _rule_keys(source), _rule_keys(oracle)

    def render(key, indexes):
        return {
            "rule_set": key[0],
            "method": key[1],
            "rule_index": indexes[0],
            "arg": key[2],
        }

    source_only = [render(k, v) for k, v in left.items() if k not in right]
    oracle_only = [render(k, v) for k, v in right.items() if k not in left]
    source_only.sort(key=lambda x: (x["rule_set"], x["rule_index"]))
    oracle_only.sort(key=lambda x: (x["rule_set"], x["rule_index"]))
    return {
        "source_rules": sum(len(v) for v in left.values()),
        "oracle_rules": sum(len(v) for v in right.values()),
        "source_only": source_only,
        "oracle_only": oracle_only,
    }


def filler_binary(build):
    """The legacy in-tree X86Tests driver under *build*, or None."""
    candidates = [
        os.path.join(build, "unittests", "Target", "X86", "X86Tests"),
        os.path.join(build, "bin", "X86Tests"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def resolve_oracle_binary(build=None, binary=None):
    """Resolve the oracle filler.

    Order: explicit ``--oracle-binary``, ``X86_MIR_ORACLE``, ``x86-mir-oracle``
    on PATH, then the legacy in-tree ``X86Tests`` under ``--build``.
    Returns ``(path, legacy)``.
    """
    if binary:
        return os.path.abspath(binary), False
    env = os.environ.get("X86_MIR_ORACLE")
    if env:
        return os.path.abspath(env), False
    found = shutil.which("x86-mir-oracle")
    if found:
        return found, False
    if build:
        legacy = filler_binary(build)
        if legacy:
            return legacy, True
    raise SystemExit(
        "no oracle filler found: pass --oracle-binary, set X86_MIR_ORACLE, "
        "put x86-mir-oracle on PATH, or pass --build for the legacy X86Tests")


def run_reference_filler(build=None, points_path=None, out_path=None,
                         binary=None):
    """Fill expected actions with the frozen C++ reference.

    The filler reads the points file, asks
    X86LegalizerInfoReference::getAction(), and writes the corpus.  This
    function only launches it and reports its exit code.  Sidecar binaries are
    invoked directly; the legacy in-tree driver needs the gtest filter.
    """
    executable, legacy = resolve_oracle_binary(build=build, binary=binary)
    if legacy and not os.path.isabs(executable):
        executable = os.path.abspath(executable)
    env = dict(os.environ)
    env["X86_LEGALIZER_CPP_POINTS"] = os.path.abspath(points_path)
    env["X86_LEGALIZER_CPP_CORPUS_OUT"] = os.path.abspath(out_path)
    command = [executable]
    cwd = None
    if legacy:
        command += ["--gtest_also_run_disabled_tests",
                    "--gtest_filter=*GenerateCorpusFromCppPoints*"]
        cwd = build
    proc = subprocess.run(command, cwd=cwd, env=env)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)
