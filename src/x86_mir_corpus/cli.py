"""Command line entry point for the X86 legalizer corpus reader."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

from . import (
    __version__,
    baseline,
    corpusdiff,
    domain,
    dsl,
    emit,
    llvm,
    mirrun,
    oracle,
    parse,
    pin,
    schemas,
    target,
    tracediff,
    triage,
    verify,
)
from . import expr as E

CORPUS_SCHEMA = schemas.CORPUS_SCHEMA
CORPUS_VERSION = schemas.CORPUS_VERSION


def _source_info(text, spec, revision, path):
    return {
        "rev": spec,
        "revision": revision,
        "path": path,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "lines": text.count("\n") + (0 if text.endswith("\n") else 1),
    }


def resolve_revision(repo, revision):
    """The full commit SHA for a symbolic revision, or *revision* if absent."""
    if not revision:
        return None
    proc = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", f"{revision}^{{commit}}"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        return revision
    return proc.stdout.strip()


def profile_for(args):
    """The target profile selected by ``--target`` (X86 by default)."""
    return target.get(getattr(args, "target", None))


def resolve_source_args(args):
    """Fill per-target defaults before reading the C++ constructor."""
    profile = profile_for(args)
    if not args.source_path:
        args.source_path = profile.source_path
    if not args.config:
        args.config = profile.default_config
    if args.config not in profile.configs:
        raise SystemExit(
            f"unknown config {args.config!r} for {profile.name}; "
            f"choose from {', '.join(sorted(profile.configs))}")
    if not args.source_rev:
        args.source_rev = (pin.revision(target=profile.name)
                           or target.DEFAULT_SOURCE_REV)
    return profile


def load_source(args):
    resolve_source_args(args)
    if args.input:
        with open(args.input, encoding="utf-8") as handle:
            text = handle.read()
        return text, _source_info(text, None, None,
                                  os.path.abspath(args.input))
    revision = args.source_rev
    spec = f"{revision}:{args.source_path}"
    proc = subprocess.run(
        ["git", "-C", args.repo, "show", spec],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.strip() or f"git show {spec} failed")
    text = proc.stdout
    return text, _source_info(text, revision,
                              resolve_revision(args.repo, revision),
                              args.source_path)


def read_revision(repo, revision, path):
    proc = subprocess.run(
        ["git", "-C", repo, "show", f"{revision}:{path}"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    return proc.stdout


def parse_input(args):
    text, source = load_source(args)
    profile = profile_for(args)
    features = profile.configs[args.config]
    result = parse.parse_source(text, source, args.config, features,
                                profile.rule_set_prefix, profile.llt_extended)
    if (result.unknown_methods or result.unknown_predicates) and not args.allow_opaque:
        for method, line in result.unknown_methods:
            print(f"unknown chain method {method} at line {line}", file=sys.stderr)
        for predicate, line in result.unknown_predicates:
            print(f"unknown predicate {predicate} at line {line}",
                  file=sys.stderr)
        raise SystemExit(2)
    return result, source


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def coverage_gate(coverage, allow_gaps):
    gaps = coverage["rules"]["unprobed"]
    uncovered = coverage["opcodes"]["uncovered"]
    if allow_gaps or (not gaps and not uncovered):
        return
    for gap in gaps:
        print(f"coverage gap: {gap['rule_set']} rule {gap['rule_index']} "
              f"{gap['method']} ({gap['kind']}): {gap['reason']}",
              file=sys.stderr)
    for gap in uncovered:
        print(f"coverage gap: {gap['rule_set']} opcode {gap['opcode']} has no "
              f"rule/boundary/AS point", file=sys.stderr)
    raise SystemExit(3)


def cmd_parse(args):
    result, source = parse_input(args)
    emit.write_json(args.out, emit.ast_document(result, source))
    census = result.census()
    print(json.dumps({"out": args.out, **census}, sort_keys=True))
    if result.unknown_refs:
        print(f"{len(result.unknown_refs)} unresolved references; see ast.json",
              file=sys.stderr)


def cmd_points(args):
    result, source = parse_input(args)
    exceptions = domain.load_coverage_exceptions(args.coverage_exceptions)
    profile = profile_for(args)
    points, classes, mem_classes, panel, pruned, coverage = domain.build_points(
        result, args.tier, exceptions, profile)
    coverage_gate(coverage, args.allow_coverage_gaps)
    doc = emit.points_document(
        result, source, args.tier, points, classes, mem_classes, panel, pruned,
        coverage=coverage, oracle=oracle.oracle_doc("none"),
        config_descriptor=profile.config_descriptor(args.config))
    emit.write_json(args.out, doc)
    print(json.dumps({"out": args.out, "tier": args.tier,
                      "points": len(points), "pruned": doc["pruned"],
                      "origins": doc["origin_counts"],
                      "rules_verified": coverage["rules"]["verified"],
                      "rules_unprobed": len(coverage["rules"]["unprobed"])},
                     sort_keys=True))


def cmd_verify(args):
    with open(args.ast, encoding="utf-8") as handle:
        ast = json.load(handle)
    if ast.get("config") != "C2":
        raise SystemExit(
            "verify compares the unconditional TableGen rule emission; "
            f"re-run parse with --config C2 (got {ast.get('config')})")
    with open(args.td_inc, encoding="utf-8") as handle:
        td = verify.parse_td_inc(handle.read())
    report = verify.build_report(ast, td)
    known = set()
    registry = args.known_deltas
    if registry is None:
        package = os.path.dirname(os.path.abspath(__file__))
        registry = os.path.join(package, "assets", "known_deltas.json")
        if not os.path.exists(registry):
            registry = os.path.join(package, "known_deltas.json")
        if not os.path.exists(registry):
            registry = None
    if registry:
        with open(registry, encoding="utf-8") as handle:
            data = json.load(handle)
        known = {(x["rule_set"], x["class"]) for x in data.get("deltas", [])}
    report["known_deltas_used"] = sorted(f"{a}:{b}" for a, b in known)
    unexplained = verify.only_allowed_deltas(report, known)
    report["unexplained"] = unexplained
    emit.write_json(args.out, report)
    print(json.dumps({
        "out": args.out,
        "census": report["census"],
        "opcodes": report["opcodes_vs_td"],
        "classes_cpp": len(report["classes_vs_td"]["cpp"]),
        "classes_td": len(report["classes_vs_td"]["td"]),
        "unexplained_deltas": len(unexplained),
    }, sort_keys=True))
    if unexplained:
        raise SystemExit(3)


def _cross_revision_delta(args, result, oracle_revision, source):
    text = read_revision(args.repo, oracle_revision, args.source_path)
    if text is None:
        raise SystemExit(
            f"cannot read the oracle source {oracle_revision} from {args.repo}")
    info = _source_info(text, oracle_revision,
                        resolve_revision(args.repo, oracle_revision),
                        args.source_path)
    profile = profile_for(args)
    oracle_result = parse.parse_source(
        text, info, args.config, profile.configs[args.config],
        profile.rule_set_prefix, profile.llt_extended)
    delta = oracle.rule_delta(result, oracle_result)
    return delta


_VEC_HEAD = re.compile(r"^(?:nxv|v)\d+")


def _canonical_llt(name, pointer_bits):
    """Match the C++ witnessLLTName spelling: pointers carry their width."""
    core = name
    head = _VEC_HEAD.match(name)
    if head:
        core = name[head.end():]
    if re.fullmatch(r"p\d+", core):
        return f"{name}:{pointer_bits}"
    return name


def _corpus_atom(point, pointer_bits=64):
    """The corpus atom shape, shared by shapes-only and filled corpora."""
    query = {"types": [_canonical_llt(t, pointer_bits)
                       for t in point["types"]]}
    if point.get("mem") is not None:
        query["mem"] = {"type": _canonical_llt(point["mem"], pointer_bits),
                        "align": point.get("align", 0)}
    return {
        "id": point["id"],
        "origin": point["origin"],
        "rule_set": point["rule_set"],
        "opcode": point["opcode"],
        "rule_index": point["rule_index"],
        "config": None,
        "query": query,
    }


def _shapes_corpus(args, source, doc, coverage, oracle_doc, points):
    profile = profile_for(args)
    config = profile.config_descriptor(args.config)
    pointer_bits = profile.pointer_bits(args.config)
    atoms = []
    for point in points:
        atom = _corpus_atom(point, pointer_bits)
        atom["config"] = config["id"]
        atoms.append(atom)
    return {
        "schema": CORPUS_SCHEMA,
        "version": CORPUS_VERSION,
        "source": source,
        "oracle": oracle_doc,
        "config": config["id"],
        "config_descriptor": config,
        "coverage": coverage,
        "counts": doc["counts"],
        "points_sha256": None,
        "atom_count": len(atoms),
        "atoms": atoms,
    }


def cmd_corpus(args):
    result, source = parse_input(args)
    exceptions = domain.load_coverage_exceptions(args.coverage_exceptions)
    points, classes, mem_classes, panel, pruned, coverage = domain.build_points(
        result, args.tier, exceptions, profile_for(args))
    coverage_gate(coverage, args.allow_coverage_gaps)

    kind = args.oracle
    if kind is None:
        kind = "reference" if (args.build or args.oracle_binary) else None
    source_revision = source.get("revision")
    cross = False
    delta = None
    oracle_doc = None

    if kind is None:
        oracle_doc = None
    elif kind == "none":
        oracle_doc = oracle.oracle_doc("none")
    elif kind == "reference":
        reference_spec = oracle.reference_revision(args.repo)
        reference = resolve_revision(args.repo, reference_spec)
        if reference is None:
            raise SystemExit(
                "cannot find the frozen reference provenance under "
                f"{args.repo}")
        cross = source_revision != reference
        if cross:
            delta = _cross_revision_delta(args, result, reference, source)
            if not args.allow_cross_revision:
                missing = ", ".join(
                    f"{x['rule_set']}#{x['rule_index']} {x['method']}"
                    for x in delta["oracle_only"])
                raise SystemExit(
                    f"cross-revision corpus: source {source_revision or '<input>'} "
                    f"!= oracle reference {reference}"
                    + (f"; oracle-only rules: {missing}" if missing else "")
                    + "; pass --allow-cross-revision to mark the corpus")
        oracle_doc = oracle.oracle_doc("reference", reference, cross,
                                       spec=reference_spec)
    else:
        raise SystemExit(f"unknown oracle {kind}")

    points_path = os.path.abspath(args.points_out or (args.out + ".points.json"))
    doc = emit.points_document(
        result, source, args.tier, points, classes, mem_classes, panel, pruned,
        coverage=coverage, oracle=oracle_doc,
        config_descriptor=profile_for(args).config_descriptor(args.config))
    emit.write_json(points_path, doc)

    if kind is None:
        print(f"points written to {points_path}; pass --build to fill expected, "
              "or --oracle=none for a shapes-only corpus", file=sys.stderr)
        return

    if kind == "none":
        corpus = _shapes_corpus(args, source, doc, coverage, oracle_doc, points)
        corpus["points_sha256"] = file_sha256(points_path)
        emit.write_json(args.out, corpus)
        print(json.dumps({
            "points": points_path, "corpus": os.path.abspath(args.out),
            "oracle": "none", "atoms": len(points),
            "rules_verified": coverage["rules"]["verified"],
        }, sort_keys=True))
        return

    if not args.build and not args.oracle_binary:
        raise SystemExit(
            "--oracle=reference requires --build or --oracle-binary")
    if args.oracle_binary:
        oracle.run_reference_filler(args.build, points_path, args.out,
                                    binary=args.oracle_binary)
    else:
        oracle.run_reference_filler(args.build, points_path, args.out)

    with open(args.out, encoding="utf-8") as handle:
        filled = json.load(handle)
    if filled.get("schema") != CORPUS_SCHEMA:
        raise SystemExit(f"{args.out} is not a {CORPUS_SCHEMA} document")
    if filled.get("atom_count") != len(points):
        raise SystemExit(
            f"filler wrote {filled.get('atom_count')} atoms, expected "
            f"{len(points)}")

    report = {
        "schema": "llvm-x86-legalizer-corpus-report",
        "version": CORPUS_VERSION,
        "source": source,
        "oracle": oracle_doc,
        "coverage": coverage,
        "counts": doc["counts"],
        "corpus": os.path.abspath(args.out),
        "points_sha256": file_sha256(points_path),
    }
    if delta is not None:
        report["source_oracle_delta"] = delta
    emit.write_json(args.out + ".report.json", report)
    print(json.dumps({
        "points": points_path, "corpus": os.path.abspath(args.out),
        "report": os.path.abspath(args.out + ".report.json"),
        "oracle": "reference", "cross_revision": cross,
        "atoms": filled["atom_count"],
        "rules_verified": coverage["rules"]["verified"],
    }, sort_keys=True))


def cmd_diff(args):
    left = corpusdiff.load(args.left)
    right = corpusdiff.load(args.right)
    report = corpusdiff.compare(left, right, "left", "right")
    report["left"]["path"] = os.path.abspath(args.left)
    report["right"]["path"] = os.path.abspath(args.right)
    emit.write_json(args.out, report)
    summary = {
        "out": os.path.abspath(args.out),
        "config_match": report["config_match"],
        "keys": report["keys"],
        "expected_available": report["expected_available"],
        "expected_diff": len(report["expected_diff"]),
    }
    print(json.dumps(summary, sort_keys=True))
    if args.strict and corpusdiff.has_differences(report):
        raise SystemExit(1)


def cmd_trace_diff(args):
    left = tracediff.load(args.left)
    right = tracediff.load(args.right)
    report = tracediff.compare(left, right, "left", "right", args.max_diffs)
    report["left"]["path"] = os.path.abspath(args.left)
    report["right"]["path"] = os.path.abspath(args.right)
    emit.write_json(args.out, report)
    summary = {
        "out": os.path.abspath(args.out),
        "corpus_match": report["corpus_match"],
        "config_match": report["config_match"],
        "ids": report["ids"],
        "status": report["status_histogram"],
        "longest_rounds": report["longest_rounds"],
        "diff_count": report["diff_count"],
    }
    print(json.dumps(summary, sort_keys=True))
    if args.strict and tracediff.has_differences(report):
        raise SystemExit(1)


def cmd_migrate(args):
    doc = corpusdiff.load(args.input)
    candidates = ([args.source_rev] if args.source_rev
                  else [doc.get("source_commit"),
                        oracle.reference_revision(args.repo),
                        target.DEFAULT_SOURCE_REV])
    source_rev = None
    source = None
    result = None
    last_error = None
    for revision in candidates:
        if not revision:
            continue
        text = read_revision(args.repo, revision, args.source_path)
        if text is None:
            last_error = f"cannot read {revision}:{args.source_path}"
            continue
        info = _source_info(text, revision,
                            resolve_revision(args.repo, revision),
                            args.source_path)
        try:
            result = parse.parse_source(text, info, args.config,
                                        target.CONFIGS[args.config])
        except E.ExprError as err:
            last_error = f"{revision}: {err}"
            continue
        source_rev = revision
        source = info
        break
    if result is None:
        raise SystemExit(last_error or "cannot find a source for the opcode map")
    rule_opcodes = {}
    for builder in result.builders:
        for opcode in builder.opcodes:
            rule_opcodes.setdefault("X86_" + opcode[2:], opcode)
        rule_opcodes.setdefault(builder.name, builder.opcodes[0])
    try:
        migrated = corpusdiff.migrate_v1(doc, rule_opcodes, source)
    except ValueError as err:
        raise SystemExit(str(err)) from err
    if migrated is None:
        raise SystemExit(f"{args.input} is not a version-1 document")
    emit.write_json(args.out, migrated)
    print(json.dumps({
        "out": os.path.abspath(args.out),
        "source_rev": source_rev,
        "atoms": migrated["atom_count"],
        "config": migrated.get("config"),
    }, sort_keys=True))


def cmd_mir_run(args):
    try:
        report, code = mirrun.run(
            args.corpus, args.out, build=args.build, driver=args.driver,
            rules=args.rules, shard_size=args.shard_size,
            timeout=args.timeout, max_steps=args.max_steps,
            only_atom=args.only_atom, a2_probe=args.a2_probe,
            jobs=args.jobs)
    except mirrun.MirRunInfraError as err:
        print(f"mir-run infrastructure error: {err}", file=sys.stderr)
        raise SystemExit(2) from None
    summary = {
        "out": os.path.abspath(args.out),
        "atoms": report["corpus"]["atoms"],
        "counts": report["counts"],
        "first_anomaly": (None if report["first_anomaly"] is None
                          else report["first_anomaly"]["id"]),
        "a3": report["a3"]["status"],
        "libcalls": report["libcalls"],
    }
    print(json.dumps(summary, sort_keys=True))
    if code:
        raise SystemExit(code)


def cmd_doctor(args):
    report = llvm.doctor(args.llvm_src, args.llvm_build,
                         args.sidecar_build,
                         allow_unpinned=args.allow_unpinned)
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        for reason in report["reasons"]:
            print(f"error: {reason}", file=sys.stderr)
        print(f"doctor: {'ok' if report['ok'] else 'not ok'} "
              f"(python {report['python']['version']})")
    if not report["ok"]:
        raise SystemExit(1)


def cmd_baseline_capture(args):
    doc = baseline.capture(args.report, args.out)
    print(json.dumps({"out": os.path.abspath(args.out),
                      "atoms": doc["atom_count"],
                      "config": doc["config"]}, sort_keys=True))


def cmd_baseline_check(args):
    report = baseline.check(args.report, args.baseline)
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(f"baseline: {'ok' if report['ok'] else 'changed'} "
              f"(added {len(report['added'])}, removed "
              f"{len(report['removed'])})")
    if not report["ok"]:
        raise SystemExit(1)


def cmd_triage_cluster(args):
    payload = triage.cluster(args.report, args.corpus, args.out,
                             driver=args.driver, build=args.build,
                             markdown=args.markdown)
    print(json.dumps({
        "out": os.path.abspath(args.out),
        "crash_clusters": payload["crash_cluster_count"],
        "budget_clusters": payload["budget_cluster_count"],
    }, sort_keys=True))


def cmd_triage_verify(args):
    payload = triage.verify(args.clusters, args.corpus, args.out,
                            driver=args.driver, build=args.build)
    print(json.dumps({"out": os.path.abspath(args.out),
                      "checks": payload["n_checks"],
                      "all_match": payload["all_match"]}, sort_keys=True))
    if not payload["all_match"]:
        raise SystemExit(1)


def cmd_pin_show(args):
    doc = pin.load(target=args.target)
    if doc is None:
        print("pin: missing or unreadable", file=sys.stderr)
        raise SystemExit(1)
    if args.json:
        print(json.dumps(doc, sort_keys=True))
        return
    print(f"target:    {args.target}")
    print(f"revision:  {doc.get('revision')}")
    print(f"snapshot:  {doc.get('snapshot')}")
    print(f"sha256:    {doc.get('snapshot_sha256')}")
    print(f"reference: {doc.get('reference_revision')}")
    print(f"recorded:  {doc.get('recorded')}")


def cmd_pin_verify(args):
    report = pin.verify(repo=args.repo, target=args.target)
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        for entry in report["checks"]:
            status = "ok" if entry["ok"] else "FAIL"
            print(f"{status:4s}  {entry['check']}")
        print(f"pin: {'ok' if report['ok'] else 'not ok'}")
    if not report["ok"]:
        raise SystemExit(1)


def cmd_pin_update(args):
    try:
        changes = pin.update(args.repo, args.revision, dry_run=args.dry_run,
                             target=args.target)
    except RuntimeError as err:
        print(f"pin update failed: {err}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps(changes, sort_keys=True))


def cmd_dsl_verify(args):
    report = dsl.verify(
        llvm_src=args.llvm_src, repo=args.repo, revision=args.revision,
        strict_source=args.strict_source)
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        for entry in report["checks"]:
            status = "ok" if entry["ok"] else "FAIL"
            print(f"{status:4s}  {entry['check']}")
        counts = report.get("counts") or {}
        if counts:
            print("counts: " + ", ".join(
                f"{key}={value}" for key, value in sorted(counts.items())))
        print(f"dsl symbols: {'ok' if report['ok'] else 'not ok'}")
    if not report["ok"]:
        raise SystemExit(1)


def cmd_dsl_report(args):
    doc = dsl.load(args.table)
    if args.json:
        print(json.dumps(doc, sort_keys=True))
        return
    print("counts: " + ", ".join(
        f"{key}={value}" for key, value in sorted(doc["counts"].items())))
    print(f"revision: {doc.get('revision')}")
    if args.methods:
        print("methods:")
        for entry in doc["methods"]:
            slots = ",".join(entry["slot_args"]) or "-"
            print(f"  {entry['name']:42s} {entry['kind']:15s} "
                  f"{entry['domain']:8s} {entry['parser']:7s} {slots}")
    if args.predicates:
        print("predicates:")
        for entry in doc["predicates"]:
            print(f"  {entry['name']:42s} {entry['domain']:8s} "
                  f"arity={entry['arity']}")


def cmd_llvm_build(args):
    binaries = llvm.build_sidecar(
        args.llvm_build, args.out, llvm_src=args.llvm_src,
        generator=args.generator, debug=args.debug)
    print(json.dumps({
        "out": os.path.abspath(args.out),
        "binaries": binaries,
    }, sort_keys=True))


def build_parser():
    p = argparse.ArgumentParser(
        prog="x86-mir-corpus",
        description="X86 legalizer corpus reader and MIR legalization corpus supervisor")
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--input", help="read this C++ file instead of a revision")
        sp.add_argument("--target", default="x86", choices=target.names(),
                        help="legalizer target (default: x86)")
        sp.add_argument("--source-rev", default=None,
                        help="git revision to read (default: the target's "
                             "pinned commit; see `pin show`)")
        sp.add_argument("--source-path", default=None,
                        help="constructor source path (default: the target's)")
        sp.add_argument("--repo",
                        default=os.environ.get("X86_MIR_LLVM_SRC", "."))
        sp.add_argument("--config", default=None,
                        help="target config id (default: the target's)")
        sp.add_argument("--allow-opaque", action="store_true",
                        help="do not fail on an unknown chain method")

    def coverage(sp):
        sp.add_argument("--coverage-exceptions",
                        help="JSON file of reviewed coverage exceptions")
        sp.add_argument("--allow-coverage-gaps", action="store_true",
                        help="emit points despite unregistered coverage gaps "
                             "(inspection only)")

    sp = sub.add_parser("dsl",
                        help="inspect or verify the legalizer DSL symbol table")
    dsl_sub = sp.add_subparsers(dest="dsl_cmd", required=True)
    dp = dsl_sub.add_parser("verify", help="check the table against the headers")
    dp.add_argument("--repo", default=os.environ.get("X86_MIR_LLVM_SRC"))
    dp.add_argument("--llvm-src")
    dp.add_argument("--revision", default=pin.default_source_rev(),
                    help="git revision to read (default: the pinned commit)")
    dp.add_argument("--strict-source", action="store_true",
                    help="fail when the header hashes differ from the table")
    dp.add_argument("--json", action="store_true")
    dp.set_defaults(func=cmd_dsl_verify)
    dp = dsl_sub.add_parser("report", help="print the symbol table")
    dp.add_argument("--table", help="symbol table JSON (default: bundled)")
    dp.add_argument("--methods", action="store_true")
    dp.add_argument("--predicates", action="store_true")
    dp.add_argument("--json", action="store_true")
    dp.set_defaults(func=cmd_dsl_report)

    sp = sub.add_parser("baseline")
    baseline_sub = sp.add_subparsers(dest="baseline_cmd", required=True)
    bp = baseline_sub.add_parser("capture")
    bp.add_argument("--report", required=True)
    bp.add_argument("--out", required=True)
    bp.set_defaults(func=cmd_baseline_capture)
    bp = baseline_sub.add_parser("check")
    bp.add_argument("--report", required=True)
    bp.add_argument("--baseline", required=True)
    bp.add_argument("--json", action="store_true")
    bp.set_defaults(func=cmd_baseline_check)

    sp = sub.add_parser("triage")
    triage_sub = sp.add_subparsers(dest="triage_cmd", required=True)
    tp = triage_sub.add_parser("cluster")
    tp.add_argument("--report", required=True)
    tp.add_argument("--corpus", required=True)
    tp.add_argument("--out", required=True)
    tp.add_argument("--driver")
    tp.add_argument("--build")
    tp.add_argument("--markdown")
    tp.set_defaults(func=cmd_triage_cluster)
    tp = triage_sub.add_parser("verify")
    tp.add_argument("--clusters", required=True)
    tp.add_argument("--corpus", required=True)
    tp.add_argument("--out", required=True)
    tp.add_argument("--driver")
    tp.add_argument("--build")
    tp.set_defaults(func=cmd_triage_verify)

    sp = sub.add_parser("doctor")
    sp.add_argument("--llvm-src")
    sp.add_argument("--llvm-build")
    sp.add_argument("--sidecar-build")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--allow-unpinned", action="store_true")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("pin",
                        help="inspect or move the pinned LLVM revision")
    pin_sub = sp.add_subparsers(dest="pin_cmd", required=True)

    pp = pin_sub.add_parser("show", help="print the pinned revision")
    pp.add_argument("--target", default="x86", choices=sorted(pin.TARGETS))
    pp.add_argument("--json", action="store_true")
    pp.set_defaults(func=cmd_pin_show)

    pp = pin_sub.add_parser("verify",
                            help="check the pin against snapshots and a checkout")
    pp.add_argument("--target", default="x86", choices=sorted(pin.TARGETS))
    pp.add_argument("--repo", default=os.environ.get("X86_MIR_LLVM_SRC"))
    pp.add_argument("--json", action="store_true")
    pp.set_defaults(func=cmd_pin_verify)

    pp = pin_sub.add_parser("update", help="re-pin from an LLVM checkout")
    pp.add_argument("--target", default="x86", choices=sorted(pin.TARGETS))
    pp.add_argument("--repo", default=os.environ.get("X86_MIR_LLVM_SRC"))
    pp.add_argument("--revision", default="upstream/main",
                    help="commit-ish to pin (default: upstream/main)")
    pp.add_argument("--dry-run", action="store_true",
                    help="print the planned changes without writing")
    pp.set_defaults(func=cmd_pin_update)

    sp = sub.add_parser("llvm")
    llvm_sub = sp.add_subparsers(dest="llvm_cmd", required=True)

    dp = llvm_sub.add_parser("doctor")
    dp.add_argument("--llvm-src")
    dp.add_argument("--llvm-build")
    dp.add_argument("--sidecar-build")
    dp.add_argument("--json", action="store_true")
    dp.add_argument("--allow-unpinned", action="store_true")
    dp.set_defaults(func=cmd_doctor)

    bp = llvm_sub.add_parser("build")
    bp.add_argument("--llvm-src")
    bp.add_argument("--llvm-build", required=True)
    bp.add_argument("--out", required=True)
    bp.add_argument("--generator", default="Ninja")
    bp.add_argument("--debug", action="store_true")
    bp.set_defaults(func=cmd_llvm_build)

    sp = sub.add_parser("parse")
    common(sp)
    sp.add_argument("--out", required=True)
    sp.set_defaults(func=cmd_parse)

    sp = sub.add_parser("points")
    common(sp)
    coverage(sp)
    sp.add_argument("--tier", default="witness",
                    choices=("witness", "full", "deep"))
    sp.add_argument("--out", required=True)
    sp.set_defaults(func=cmd_points)

    sp = sub.add_parser("verify")
    sp.add_argument("--ast", required=True)
    sp.add_argument("--td-inc", required=True)
    sp.add_argument("--known-deltas")
    sp.add_argument("--out", required=True)
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("corpus")
    common(sp)
    coverage(sp)
    sp.add_argument("--tier", default="witness",
                    choices=("witness", "full", "deep"))
    sp.add_argument("--oracle", choices=("none", "reference"),
                    help="expected-action source (default: reference when "
                         "--build or --oracle-binary is given, otherwise "
                         "points-only)")
    sp.add_argument("--oracle-binary",
                    help="sidecar oracle binary (default: x86-mir-oracle on "
                         "PATH, then $X86_MIR_ORACLE, then legacy X86Tests)")
    sp.add_argument("--allow-cross-revision", action="store_true",
                    help="allow source.revision != oracle.revision; the "
                         "corpus is marked cross_revision")
    sp.add_argument("--build", help="LLVM build dir containing X86Tests")
    sp.add_argument("--points-out")
    sp.add_argument("--out", required=True)
    sp.set_defaults(func=cmd_corpus)

    sp = sub.add_parser("migrate")
    sp.add_argument("--input", required=True)
    sp.add_argument("--out", required=True)
    sp.add_argument("--source-rev",
                    help="source for the rule-set -> primary-opcode map "
                         "(default: source_commit, then the frozen reference)")
    sp.add_argument("--source-path", default=target.DEFAULT_SOURCE_PATH)
    sp.add_argument("--repo",
                    default=os.environ.get("X86_MIR_LLVM_SRC", "."))
    sp.add_argument("--config", default=target.DEFAULT_CONFIG,
                    choices=sorted(target.CONFIGS))
    sp.set_defaults(func=cmd_migrate)

    sp = sub.add_parser("trace-diff")
    sp.add_argument("--left", required=True,
                    help="trace to compare from (the reference)")
    sp.add_argument("--right", required=True,
                    help="trace to compare against (the table)")
    sp.add_argument("--out", required=True)
    sp.add_argument("--max-diffs", type=int, default=50,
                    help="cap the per-diff detail and id samples (default: 50)")
    sp.add_argument("--strict", action="store_true",
                    help="exit 1 when ids, statuses, rounds, or traces differ")
    sp.set_defaults(func=cmd_trace_diff)

    sp = sub.add_parser("mir-run")
    sp.add_argument("--corpus", required=True,
                    help="filled corpus JSON from the corpus subcommand")
    sp.add_argument("--build", help="LLVM build dir containing X86Tests")
    sp.add_argument("--driver", help="explicit X86Tests binary")
    sp.add_argument("--rules", default="production",
                    choices=("production", "reference"))
    sp.add_argument("--shard-size", type=int, default=2000)
    sp.add_argument("--timeout", type=float, default=120,
                    help="per-shard timeout in seconds")
    sp.add_argument("--max-steps", type=int, default=10000)
    sp.add_argument("--jobs", type=int, default=1,
                    help="run up to N shards concurrently (default: 1)")
    sp.add_argument("--only-atom", help="run exactly one atom id")
    sp.add_argument("--a2-probe", action="store_true",
                    help="mark this as the fixed-rules A2 probe (A3 exempt)")
    sp.add_argument("--out", required=True)
    sp.set_defaults(func=cmd_mir_run)

    sp = sub.add_parser("diff")
    sp.add_argument("--left", required=True)
    sp.add_argument("--right", required=True)
    sp.add_argument("--out", required=True)
    sp.add_argument("--strict", action="store_true",
                    help="exit 1 when keys, expected actions, or configs differ")
    sp.set_defaults(func=cmd_diff)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0
