"""Cluster a mir-run report and replay one representative per cluster.

Clustering is read-only and deterministic: no timestamps, sorted JSON, and
stable representative order.  ``expected`` is used here only; the driver never
reads it.
"""

import collections
import json
import os
import re
import shlex
import subprocess
import sys

from . import schemas

ASSERT_RE = re.compile(r"(\S+\.(?:cpp|h)):(\d+):\s*(.*?):\s*Assertion `([^']*)")
_VEC_RE = re.compile(r"^(nxv|v)(\d+)(.+)$")
_ELEM_RE = re.compile(r"^(p|s|i|f)(\d+)$|^(bf16|ppcf128)$")


def norm_type(text):
    """Canonical corpus type name: fixed/scalable vector, any scalar kind.

    The pointer-width suffix is dropped here; ``parseCorpusLLT`` re-attaches it
    from the target data layout.
    """
    core, sep, suffix = text.rpartition(":")
    if not sep or not suffix.isdigit():
        core = text
    match = _VEC_RE.match(core)
    if match:
        return f"{match.group(1)}{int(match.group(2))}{norm_type(match.group(3))}"
    match = _ELEM_RE.match(core)
    if match:
        if match.group(1):
            return f"{match.group(1)}{int(match.group(2))}"
        return match.group(3)
    return text


def type_shape(atom):
    query = atom.get("query") or {}
    return tuple(norm_type(t) for t in query.get("types") or ())


def crash_site(atom):
    text = atom.get("stderr_tail") or ""
    match = ASSERT_RE.search(text)
    if not match:
        return ("unknown", 0, atom.get("detail") or "", "")
    path, line, function, expression = match.groups()
    return (path.rsplit("/", 1)[-1], int(line), function, expression)


def _expected_action(atoms, atom_id):
    return ((atoms.get(atom_id) or {}).get("expected") or {}).get("action")


def repro_command(atom_id, corpus_path, driver=None, build=None):
    parts = [sys.executable, "-m", "x86_mir_corpus", "mir-run",
             "--corpus", os.path.abspath(corpus_path),
             "--rules", "production",
             "--only-atom", atom_id,
             "--max-steps", "10000", "--timeout", "120",
             "--out", "/tmp/atom.json"]
    if driver:
        parts += ["--driver", driver]
    elif build:
        parts += ["--build", build]
    return " ".join(shlex.quote(part) for part in parts)


def cluster_report(report, corpus, corpus_path, driver=None, build=None):
    atoms = {atom["id"]: atom for atom in corpus["atoms"]}
    crashes = [a for a in report["atoms"] if a["outcome"] == "crash"]
    budgets = [a for a in report["atoms"] if a["outcome"] == "budget"]

    def repro(atom_id):
        return repro_command(atom_id, corpus_path, driver=driver, build=build)

    by_site = collections.OrderedDict()
    for atom in crashes:
        site = crash_site(atom)
        cluster = by_site.setdefault(site, {
            "assert_site": f"{site[0]}:{site[1]}",
            "function": site[2],
            "expression": site[3],
            "count": 0,
            "opcodes": collections.Counter(),
            "type_shapes": collections.Counter(),
            "expected_actions": collections.Counter(),
            "representatives": [],
        })
        cluster["count"] += 1
        cluster["opcodes"][atom.get("opcode")] += 1
        cluster["type_shapes"][type_shape(atoms.get(atom["id"]) or atom)] += 1
        cluster["expected_actions"][_expected_action(atoms, atom["id"])] += 1
        if len(cluster["representatives"]) < 5:
            cluster["representatives"].append({
                "id": atom["id"],
                "opcode": atom.get("opcode"),
                "types": (atoms.get(atom["id"]) or {}).get("query", {})
                         .get("types"),
                "expected": _expected_action(atoms, atom["id"]),
                "repro": repro(atom["id"]),
            })

    crash_clusters = []
    for cluster in sorted(by_site.values(), key=lambda c: -c["count"]):
        cluster["opcodes"] = dict(cluster["opcodes"].most_common())
        cluster["type_shapes"] = [
            {"shape": list(shape), "count": count}
            for shape, count in cluster["type_shapes"].most_common(8)]
        cluster["expected_actions"] = dict(
            cluster["expected_actions"].most_common())
        crash_clusters.append(cluster)

    by_shape = collections.OrderedDict()
    for atom in budgets:
        key = (atom.get("opcode"), type_shape(atoms.get(atom["id"]) or atom))
        cluster = by_shape.setdefault(key, {
            "opcode": key[0],
            "type_shape": list(key[1]),
            "count": 0,
            "rule_sets": collections.Counter(),
            "expected_actions": collections.Counter(),
            "representatives": [],
        })
        cluster["count"] += 1
        cluster["rule_sets"][(atoms.get(atom["id"]) or {}).get("rule_set")] += 1
        cluster["expected_actions"][_expected_action(atoms, atom["id"])] += 1
        if len(cluster["representatives"]) < 3:
            cluster["representatives"].append({
                "id": atom["id"],
                "steps": atom.get("steps"),
                "expected": _expected_action(atoms, atom["id"]),
                "repro": repro(atom["id"]),
            })

    budget_clusters = []
    for cluster in sorted(by_shape.values(), key=lambda c: -c["count"]):
        cluster["rule_sets"] = dict(cluster["rule_sets"].most_common())
        cluster["expected_actions"] = dict(
            cluster["expected_actions"].most_common())
        budget_clusters.append(cluster)

    coverage = collections.Counter(a["outcome"] for a in report["atoms"])
    return {
        "schema": schemas.TRIAGE_SCHEMA,
        "version": schemas.TRIAGE_VERSION,
        "source_report": os.path.abspath(report.get("corpus", {}).get("path", ""))
        if report.get("corpus") else None,
        "corpus": report.get("corpus"),
        "counts": dict(sorted(coverage.items())),
        "reached_legalizer": sum(coverage[kind] for kind in
                                 ("legalized", "failed", "budget", "crash")),
        "crash_cluster_count": len(crash_clusters),
        "budget_cluster_count": len(budget_clusters),
        "crash_clusters": crash_clusters,
        "budget_clusters": budget_clusters,
    }


def cluster(report_path, corpus_path, out_path, driver=None, build=None,
            markdown=None):
    with open(report_path, encoding="utf-8") as handle:
        report = json.load(handle)
    with open(corpus_path, encoding="utf-8") as handle:
        corpus = json.load(handle)
    payload = cluster_report(report, corpus, corpus_path, driver=driver,
                             build=build)
    payload["source_report"] = os.path.abspath(report_path)
    directory = os.path.dirname(os.path.abspath(out_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, sort_keys=True)
        handle.write("\n")
    if markdown:
        with open(markdown, "w", encoding="utf-8") as handle:
            handle.write(render_markdown(payload))
    return payload


def render_markdown(payload):
    total = (payload.get("corpus") or {}).get("atoms") or 0
    reached = payload["reached_legalizer"]
    percent = (100 * reached / total) if total else 0.0
    lines = [
        "# Full corpus triage",
        "",
        f"- atoms: {total}, reached legalizer: {reached} ({percent:.1f}%)",
        f"- crash clusters: {payload['crash_cluster_count']}",
        f"- budget clusters: {payload['budget_cluster_count']}",
        "",
        "## Crash clusters",
        "",
        "| # | atoms | site | function | opcodes | expected actions |",
        "|---|-------|------|----------|---------|------------------|",
    ]
    for index, cluster in enumerate(payload["crash_clusters"], 1):
        function = (cluster.get("function") or "").split("(")[0].split()
        name = function[-1] if function else "?"
        lines.append(
            f"| {index} | {cluster['count']} | `{cluster['assert_site']}` | "
            f"`{name}` | {', '.join(list(cluster['opcodes'])[:5])} | "
            f"{cluster['expected_actions']} |")
    lines += ["", "## Budget clusters", "",
              "| # | atoms | opcode | type shape | rule sets | expected actions |",
              "|---|-------|--------|------------|-----------|------------------|"]
    for index, cluster in enumerate(payload["budget_clusters"], 1):
        lines.append(
            f"| {index} | {cluster['count']} | {cluster['opcode']} | "
            f"`{cluster['type_shape']}` | {list(cluster['rule_sets'])[:2]} | "
            f"{cluster['expected_actions']} |")
    return "\n".join(lines) + "\n"


def _run_atom(corpus_path, atom_id, driver=None, build=None):
    import tempfile
    with tempfile.TemporaryDirectory(prefix="x86-mir-triage-") as tmp:
        out = os.path.join(tmp, "report.json")
        command = [sys.executable, "-m", "x86_mir_corpus", "mir-run",
                   "--corpus", corpus_path, "--rules", "production",
                   "--only-atom", atom_id, "--max-steps", "10000",
                   "--timeout", "120", "--out", out]
        if driver:
            command += ["--driver", driver]
        elif build:
            command += ["--build", build]
        proc = subprocess.run(command, capture_output=True,
                              text=True)
        if not os.path.exists(out):
            raise SystemExit(
                f"triage verify: no report for {atom_id}: "
                f"{proc.stderr.strip() or f'exit {proc.returncode}'}")
        with open(out, encoding="utf-8") as handle:
            report = json.load(handle)
    atom = next(a for a in report["atoms"] if a["id"] == atom_id)
    return proc.returncode, atom


def verify(clusters_path, corpus_path, out_path, driver=None, build=None):
    with open(clusters_path, encoding="utf-8") as handle:
        clusters = json.load(handle)
    checks = []
    for kind, key in (("crash", "crash_clusters"),
                      ("budget", "budget_clusters")):
        seen = set()
        for cluster in clusters[key]:
            tag = (cluster.get("opcode") if kind == "budget"
                   else cluster["assert_site"])
            if tag in seen:
                continue
            seen.add(tag)
            representative = cluster["representatives"][0]
            code, atom = _run_atom(corpus_path, representative["id"],
                                   driver=driver, build=build)
            checks.append({
                "kind": kind,
                "cluster": cluster.get("assert_site") or
                f"{cluster['opcode']} {cluster['type_shape']}",
                "atom": representative["id"],
                "expected": kind,
                "observed": atom["outcome"],
                "detail_code": atom["detail_code"],
                "steps": atom["steps"],
                "exit": code,
                "match": atom["outcome"] == kind,
            })
    payload = {
        "schema": schemas.TRIAGE_CHECK_SCHEMA,
        "version": schemas.TRIAGE_CHECK_VERSION,
        "checks": checks,
        "all_match": all(check["match"] for check in checks),
        "n_checks": len(checks),
    }
    directory = os.path.dirname(os.path.abspath(out_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, sort_keys=True)
        handle.write("\n")
    return payload
