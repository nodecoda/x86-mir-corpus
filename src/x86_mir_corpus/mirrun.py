"""Supervisor for the legalizer MIR corpus driver.

The supervisor is target-neutral: the corpus ``config_descriptor.target`` (or
the explicit ``--target``) selects the profile that supplies the pointer width
and known A3 families.  The historical default is ``x86``.

The C++ driver is one process per shard.  It emits one JSONL stream to
``X86_LEGALIZER_MIR_OUT`` (START/CONSTRUCTED/LEGALIZE markers, one OUTCOME per
atom, then a SUMMARY) and uses the process exit status/signal and stderr to
report the unusual cases.  This module owns the sharding, the timeout, the
classification order, and the report written for the caller.

The pure helpers (``parse_jsonl``, ``classify_shard``, ``partition_shards``,
``build_report``, ``classify_a3``, ``exit_code``) do not touch the filesystem
or subprocesses; they are the unit-test surface.
"""

import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor

from . import targets

SCHEMA = "llvm-x86-legalizer-mir-run"
VERSION = 1

TERMINAL_OUTCOMES = ("legalized", "failed", "unbuildable", "construction_mismatch")
ANOMALY_OUTCOMES = ("budget", "hang", "crash")
COUNT_KEYS = (
    "legalized", "failed", "budget", "hang", "crash",
    "unbuildable", "construction_mismatch", "not_run",
)
ALL_OUTCOMES = frozenset(TERMINAL_OUTCOMES + ANOMALY_OUTCOMES + ("not_run",))

SLOW_REASON = "slow_or_stuck_before_legalizer"
ONLY_ATOM_REASON = "only-atom"
FOLLOW_REASONS = {
    "after_budget": "budget",
    "after_hang": "hang",
    "after_crash": "crash",
}

INFRA_MARKER = "X86_LEGALIZER_MIR_INFRA_ERROR"
BUDGET_RE = re.compile(
    r"legalizer step budget exceeded:\s*atom=(\S+)\s+steps=(\d+)")
POINTER_RE = re.compile(r"p\d+(?::(\d+))?")

# Legacy X86 fallback; new corpora resolve their width from the target profile.
CONFIG_POINTER_BITS = {"C0": 32, "C1": 64, "C2": 64, "C3": 32}

# A3 known family: G_BUILD_VECTOR MoreElements atoms.  Pointers are spelled
# with their width, so both the C0/C3 and C1/C2 spellings are accepted; the
# width is additionally normalised against the atom's config.
KNOWN_BUILD_VECTOR_TYPES = frozenset(
    ("v2s8", "v2s16", "v2s32", "v3s32", "v4s8"))
KNOWN_POINTER_VECTOR_RE = re.compile(r"^v3p0(?::(\d+))?$")


class MirRunInfraError(Exception):
    """A missing binary, unreadable corpus, or malformed corpus/environment."""


# ---------------------------------------------------------------------------
# Small file/JSON helpers


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _as_text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def stderr_tail(text, limit=4000):
    """The last *limit* characters of stderr, for anomaly entries only."""
    text = _as_text(text).strip()
    if not text or len(text) <= limit:
        return text
    return "...\n" + text[-limit:]


def parse_jsonl(text):
    """Parse the driver's JSONL stream, tolerating blank/malformed lines.

    Malformed lines become ``{"event": "_parse_error"}`` events so the shard
    classifier can surface a warning instead of silently losing evidence.
    """
    events = []
    for number, line in enumerate(_as_text(text).splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            events.append({"event": "_parse_error", "line": number,
                           "text": line[:200]})
            continue
        if not isinstance(event, dict):
            events.append({"event": "_parse_error", "line": number,
                           "text": line[:200]})
            continue
        events.append(event)
    return events


# ---------------------------------------------------------------------------
# Corpus validation and driver resolution


def load_corpus(path):
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except OSError as err:
        raise MirRunInfraError(f"cannot read corpus {path}: {err}") from err
    except ValueError as err:
        raise MirRunInfraError(f"corpus {path} is not valid JSON: {err}") from err
    if not isinstance(doc, dict):
        raise MirRunInfraError(f"corpus {path} is not a JSON object")
    return doc


def corpus_config(doc):
    descriptor = doc.get("config_descriptor") or {}
    config = descriptor.get("id")
    if config is None:
        config = doc.get("config")
    if config is None:
        configs = {atom.get("config") for atom in doc.get("atoms") or ()
                   if atom.get("config")}
        if len(configs) == 1:
            config = next(iter(configs))
    return config


def corpus_target(doc):
    """The target profile name for *doc* (historical default: ``x86``)."""
    descriptor = doc.get("config_descriptor") or {}
    return descriptor.get("target") or doc.get("target") or targets.DEFAULT


def _profile(target):
    try:
        return targets.get(target)
    except ValueError as err:
        raise MirRunInfraError(str(err)) from None


def _pointer_widths(atom):
    query = atom.get("query") or {}
    types = list(query.get("types") or ())
    mem = query.get("mem") or {}
    if mem.get("type"):
        types.append(mem["type"])
    for name in types:
        match = POINTER_RE.search(name)
        if match and match.group(1):
            yield int(match.group(1))


def validate_corpus(doc):
    """Return ``(atoms, config)`` or raise on a malformed/bad-env corpus."""
    atoms = doc.get("atoms")
    if not isinstance(atoms, list):
        raise MirRunInfraError("corpus has no atoms array")
    for atom in atoms:
        if not isinstance(atom, dict) or not atom.get("id"):
            raise MirRunInfraError("corpus atom without an id")
    declared = doc.get("atom_count")
    if declared is not None and declared != len(atoms):
        raise MirRunInfraError(
            f"corpus atom_count {declared} != {len(atoms)} atoms")

    config = corpus_config(doc)
    target = corpus_target(doc)
    profile = _profile(target)
    top_level = doc.get("config")
    descriptor = (doc.get("config_descriptor") or {}).get("id")
    if (top_level is not None and descriptor is not None and
            top_level != descriptor):
        raise MirRunInfraError(
            f"corpus config {top_level!r} != config_descriptor id "
            f"{descriptor!r}")
    if config is not None and config not in profile.configs:
        raise MirRunInfraError(
            f"unknown corpus config {config!r} for target {target!r}")
    expected_width = (profile.pointer_bits(config)
                      if config is not None else None)
    if expected_width is not None:
        for atom in atoms:
            atom_config = atom.get("config")
            if atom_config is not None and atom_config != config:
                raise MirRunInfraError(
                    f"atom {atom['id']} config {atom_config!r} != corpus "
                    f"config {config!r}")
            for width in _pointer_widths(atom):
                if width != expected_width:
                    raise MirRunInfraError(
                        f"atom {atom['id']} pointer width {width} != {config} "
                        f"pointer width {expected_width}")
    return atoms, config


def driver_command(binary):
    """The argv prefix for *binary*.

    Sidecar drivers are plain executables.  The in-tree X86Tests binary is a
    gtest binary whose corpus run is a disabled test, so it needs the legacy
    filter.  Detection is by basename; sidecars are named ``mir-run`` or the
    historical ``x86-mir-run``.
    """
    name = os.path.basename(binary)
    if name in ("mir-run", "x86-mir-run") or \
            name.startswith(("mir-run.", "x86-mir-run.")):
        return [binary]
    return [binary, "--gtest_also_run_disabled_tests",
            "--gtest_filter=*RunCorpusThroughLegalizer"]


def resolve_driver(build=None, driver=None):
    """Resolve --driver or a sidecar, then the legacy X86Tests binaries."""
    if driver:
        candidates = [driver]
    else:
        candidates = []
        env = os.environ.get("X86_MIR_RUN")
        if env:
            candidates.append(env)
        found = shutil.which("mir-run") or shutil.which("x86-mir-run")
        if found:
            candidates.append(found)
        if build:
            candidates += [
                os.path.join(build, "bin", "X86Tests"),
                os.path.join(build, "unittests", "Target", "X86", "X86Tests"),
            ]
        if not candidates:
            raise MirRunInfraError("no --driver and no --build given")
    for candidate in candidates:
        candidate = os.path.abspath(candidate)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise MirRunInfraError(
        "MIR driver not found (tried: " + ", ".join(candidates) + ")")


def _write_shard_corpus(path, doc, remaining):
    """Write a compact corpus containing only ``remaining`` atoms.

    The wrapper (config, features, coverage) is copied so the driver's
    validation sees the same corpus identity; only ``atoms``/``atom_count``
    change.  Compact JSON keeps a 300-atom shard well under a megabyte.
    """
    shard_doc = dict(doc)
    shard_doc["atoms"] = [atom for _, atom in remaining]
    shard_doc["atom_count"] = len(remaining)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(shard_doc, handle, separators=(",", ":"),
                  ensure_ascii=False)
        handle.write("\n")


def _write_json(path, obj):
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(obj, indent=1, sort_keys=True,
                                ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Per-atom result helpers


def _result(atom, index, outcome, steps=0, detail_code="", detail="",
            stderr_tail_text="", reason=None, failed_on_opcode=None):
    try:
        steps = int(steps)
    except (TypeError, ValueError):
        steps = 0
    result = {
        "id": atom.get("id"),
        "index": index,
        "opcode": atom.get("opcode"),
        "outcome": outcome,
        "steps": steps,
        "detail_code": detail_code or "",
        "detail": detail or "",
        "stderr_tail": stderr_tail_text or "",
    }
    if reason is not None:
        result["reason"] = reason
    if failed_on_opcode is not None:
        result["failed_on_opcode"] = failed_on_opcode
    return result


def _not_run(atom, index, reason, stderr_tail_text=""):
    return _result(atom, index, "not_run", steps=0,
                   detail_code="not_run", detail=reason,
                   stderr_tail_text=stderr_tail_text, reason=reason)


def _crash_detail_code(returncode, stderr):
    if returncode is not None and returncode < 0:
        signum = -returncode
        if signum == getattr(signal, "SIGSEGV", 11):
            return "crash:segv"
        if signum == getattr(signal, "SIGABRT", 6):
            lowered = _as_text(stderr).lower()
            if "assert" in lowered or "llvm error" in lowered:
                return "crash:assert"
            return "crash:signal"
        return "crash:signal"
    if returncode is not None and returncode > 0:
        lowered = _as_text(stderr).lower()
        if "assert" in lowered or "llvm error" in lowered:
            return "crash:assert"
        return "crash:exit"
    return "crash:incomplete"


def _termination_detail(returncode, stderr):
    if returncode is not None and returncode < 0:
        return f"driver terminated by signal {-returncode}"
    if returncode is not None and returncode > 0:
        return f"driver exited with code {returncode} before the atom finished"
    return "driver did not report an outcome for the atom"


def _outcome_result(atom, index, event, stderr_text):
    outcome = event.get("outcome")
    detail_code = event.get("detail_code") or ""
    detail = event.get("detail") or ""
    tail = ""
    if outcome not in ALL_OUTCOMES:
        detail_code = "crash:unknown-outcome"
        detail = f"driver emitted unknown outcome {outcome!r}"
        outcome = "crash"
    if outcome in ANOMALY_OUTCOMES:
        tail = stderr_tail(stderr_text)
    reason = None
    if outcome == "not_run":
        reason = event.get("reason") or "driver"
    return _result(atom, index, outcome, steps=event.get("steps", 0),
                   detail_code=detail_code, detail=detail,
                   stderr_tail_text=tail, reason=reason,
                   failed_on_opcode=event.get("failed_on_opcode"))


# ---------------------------------------------------------------------------
# Shard classification


def classify_shard(events, shard_atoms, timed_out=False, returncode=None,
                   stderr="", max_steps=10000):
    """Classify one driver process against the atoms assigned to its shard.

    ``shard_atoms`` is ``[(global_index, atom), ...]`` in corpus order.
    Returns ``{"results": [...], "summary": event|None, "warnings": [...]}``
    with exactly one result per shard atom.

    Classification order (PRD rev 7 §4.5), highest priority first:
      1. stderr budget marker -> ``budget`` (cross-checked with START)
      2. unfinished atom after LEGALIZE -> hang on timeout / crash on signal
      2b. unfinished atom before LEGALIZE -> not_run on timeout / crash:construction
      3. all shard atoms have outcomes -> complete, even on a nonzero/slow exit
      4. otherwise the atom after the last completed one -> crash
      4. no marker at all -> not_run (timeout_before_start/no_atom_observed)
      5. any remaining shard atoms -> not_run
    """
    stderr = _as_text(stderr)
    warnings = []
    by_id = {}
    ordered = []
    for index, atom in shard_atoms:
        by_id[atom["id"]] = (index, atom)
        ordered.append(atom["id"])

    outcomes = {}
    starts = {}
    phases = {}
    current = None
    summary = None

    def result_for(atom_id):
        index, atom = by_id[atom_id]
        return _outcome_result(atom, index, outcomes[atom_id], stderr)

    for event in events:
        kind = event.get("event")
        if kind == "_parse_error":
            warnings.append(
                f"malformed JSONL line {event.get('line')}: "
                f"{event.get('text', '')!r}")
        elif kind == "start":
            atom_id = event.get("id")
            if atom_id not in by_id:
                warnings.append(f"start for unknown atom {atom_id!r}")
            if current is not None and current not in outcomes:
                warnings.append(
                    f"new start {atom_id!r} before outcome for {current!r}")
            current = atom_id
            phases[atom_id] = "start"
            starts[atom_id] = event
        elif kind in ("constructed", "legalize"):
            atom_id = event.get("id") or current
            if atom_id is None:
                warnings.append(f"{kind} marker without a started atom")
            else:
                if atom_id not in by_id:
                    warnings.append(f"{kind} for unknown atom {atom_id!r}")
                phases[atom_id] = kind
                if kind == "legalize" and current is None:
                    current = atom_id
        elif kind == "outcome":
            atom_id = event.get("id")
            if atom_id is None:
                warnings.append("outcome without an atom id")
                continue
            if atom_id not in by_id:
                warnings.append(f"outcome for unknown atom {atom_id!r}")
            outcomes[atom_id] = event
            if atom_id == current:
                current = None
        elif kind == "summary":
            summary = event
        else:
            warnings.append(f"unknown driver event {kind!r}")

    def tail():
        return stderr_tail(stderr)

    # (1) The budget marker wins over a signal or nonzero exit.
    budget = BUDGET_RE.search(stderr)
    if budget:
        budget_id = budget.group(1)
        budget_steps = int(budget.group(2))
        if budget_steps != max_steps + 1:
            warnings.append(
                f"budget marker steps {budget_steps} != max_steps+1 "
                f"({max_steps + 1})")
        marker_line = next(
            (line for line in stderr.splitlines() if BUDGET_RE.search(line)),
            budget.group(0))
        in_progress = current if current is not None and current not in outcomes \
            else None
        if in_progress is not None and in_progress != budget_id:
            warnings.append(
                f"budget marker atom {budget_id!r} != in-progress atom "
                f"{in_progress!r}")
        results = []
        seen_budget = False
        for index, atom in shard_atoms:
            atom_id = atom["id"]
            if atom_id == budget_id:
                seen_budget = True
                results.append(_result(
                    atom, index, "budget", steps=budget_steps,
                    detail_code="budget", detail=marker_line,
                    stderr_tail_text=tail()))
            elif atom_id in outcomes:
                results.append(result_for(atom_id))
            else:
                results.append(_not_run(atom, index, "after_budget"))
        if not seen_budget:
            warnings.append(
                f"budget marker atom {budget_id!r} is not in this shard")
            results = []
            for index, atom in shard_atoms:
                if atom["id"] in outcomes:
                    results.append(result_for(atom["id"]))
                else:
                    results.append(_not_run(atom, index, "budget_unknown_atom"))
        return {"results": results, "summary": summary,
                "warnings": warnings}

    # Find the (single) unfinished atom, if any.
    unfinished = None
    if current is not None and current not in outcomes:
        unfinished = current
    if unfinished is None:
        for atom_id in reversed(list(starts)):
            if atom_id not in outcomes:
                unfinished = atom_id
                break

    def finish(primary_id, primary_result, next_reason):
        results = []
        for index, atom in shard_atoms:
            atom_id = atom["id"]
            if atom_id == primary_id:
                results.append(primary_result)
            elif atom_id in outcomes:
                results.append(result_for(atom_id))
            else:
                results.append(_not_run(atom, index, next_reason))
        return {"results": results, "summary": summary,
                "warnings": warnings}

    if unfinished is not None and unfinished in by_id:
        index, atom = by_id[unfinished]
        phase = phases.get(unfinished, "start")
        if timed_out:
            if phase == "legalize":
                primary = _result(
                    atom, index, "hang", detail_code="hang:timeout",
                    detail="timeout while legalizeMachineFunction was running",
                    stderr_tail_text=tail())
                return finish(unfinished, primary, "after_hang")
            primary = _not_run(atom, index, SLOW_REASON,
                               stderr_tail_text=tail())
            warnings.append(
                f"timeout before legalize for atom {unfinished!r} "
                f"(phase={phase}); not a legalizer hang")
            return finish(unfinished, primary, "after_incomplete")
        if phase == "legalize":
            detail_code = _crash_detail_code(returncode, stderr)
            primary = _result(
                atom, index, "crash", detail_code=detail_code,
                detail=_termination_detail(returncode, stderr),
                stderr_tail_text=tail())
        else:
            primary = _result(
                atom, index, "crash", detail_code="crash:construction",
                detail=_termination_detail(returncode, stderr),
                stderr_tail_text=tail())
        return finish(unfinished, primary, "after_crash")

    # (3) No unfinished atom: a shard with every outcome is complete.
    missing = [atom_id for atom_id in ordered if atom_id not in outcomes]
    if not missing:
        if timed_out:
            warnings.append(
                "shard emitted outcomes for every atom but the process "
                "timed out afterwards; no hang reported")
        elif returncode not in (0, None):
            warnings.append(
                f"shard emitted outcomes for every atom but exited with "
                f"code {returncode}; no hang reported")
        return {"results": [result_for(atom_id) for atom_id in ordered],
                "summary": summary, "warnings": warnings}

    # (4) The process ended before any atom was observed.  A timeout here is
    # an infrastructure limit (corpus parse/pre-flight), not a legalizer
    # crash, and it must not be counted as progress by the resume loop.
    if not starts and not outcomes:
        reason = "timeout_before_start" if timed_out else "no_atom_observed"
        warnings.append(
            f"driver produced no atom markers (timed_out={timed_out}, "
            f"returncode={returncode}); reporting {reason}")
        return {"results": [_not_run(atom, index, reason)
                            for index, atom in shard_atoms],
                "summary": summary, "warnings": warnings}

    # (5) Last completed + 1 is a crash.
    completed = [pos for pos, atom_id in enumerate(ordered)
                 if atom_id in outcomes]
    last_completed = max(completed) if completed else -1
    candidates = [pos for pos in range(last_completed + 1, len(ordered))
                  if ordered[pos] not in outcomes]
    if not candidates:
        candidates = [pos for pos, atom_id in enumerate(ordered)
                      if atom_id not in outcomes]
    crash_pos = candidates[0]
    crash_id = ordered[crash_pos]
    crash_index, crash_atom = by_id[crash_id]

    # The process ended after the last complete outcome but before this atom
    # emitted any marker, so nothing was observed for it.  Attributing a
    # timeout or a termination here to the atom would fabricate a ``crash``
    # (and, worse, make the resume loop treat it as resolved and skip it).
    # Report the pre-start reason instead: ``not_run`` never counts as
    # progress, so the atom is retried from this index.
    if crash_id not in starts:
        reason = ("timeout_before_start" if timed_out
                  else "unattributed_termination")
        warnings.append(
            f"driver ended before atom {crash_id!r} emitted a start marker "
            f"(timed_out={timed_out}, returncode={returncode}); reporting "
            f"{reason}")
        return finish(crash_id,
                      _not_run(crash_atom, crash_index, reason,
                               stderr_tail_text=tail()),
                      "after_incomplete")

    primary = _result(
        crash_atom, crash_index, "crash",
        detail_code=_crash_detail_code(returncode, stderr),
        detail=_termination_detail(returncode, stderr),
        stderr_tail_text=tail())
    return finish(crash_id, primary, "after_crash")


# ---------------------------------------------------------------------------
# A3 classification


def _pointer_bits(config, target=targets.DEFAULT):
    if config is None:
        return None
    profile = _profile(target)
    if config in profile.configs:
        return profile.pointer_bits(config)
    return CONFIG_POINTER_BITS.get(config)


_AARCH64_FAMILIES = None


def aarch64_known_families():
    """Reviewed A3 families for AArch64, loaded once from the baseline asset.

    The first review (2026-11-05) found no livelock or crash family: the A2
    witness has ``crash/budget/hang/not_run = 0``.  The asset is the seam for
    adding one; every anomaly is a new finding while it stays empty.
    """
    global _AARCH64_FAMILIES
    if _AARCH64_FAMILIES is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "assets", "aarch64-baseline-witness.json")
        try:
            with open(path, encoding="utf-8") as handle:
                doc = json.load(handle)
            _AARCH64_FAMILIES = frozenset(doc.get("known_families") or ())
        except (OSError, ValueError, AttributeError):
            _AARCH64_FAMILIES = frozenset()
    return _AARCH64_FAMILIES


def is_known_family(atom, config=None, target=targets.DEFAULT):
    """True for a rule set the target's A3 baseline already accounts for.

    X86 has the reviewed BUILD_VECTOR MoreElements family.  AArch64 reads its
    reviewed list from ``assets/aarch64-baseline-witness.json``; while that
    list is empty every anomaly is reported as a new finding.
    """
    if target == "aarch64":
        return atom.get("rule_set") in aarch64_known_families()
    if target != "x86":
        return False
    if atom.get("rule_set") != "X86_BUILD_VECTOR":
        return False
    expected = atom.get("expected") or {}
    if expected.get("action") != "MoreElements":
        return False
    types = (atom.get("query") or {}).get("types") or []
    if not types:
        return False
    type0 = types[0]
    if type0 in KNOWN_BUILD_VECTOR_TYPES:
        return True
    match = KNOWN_POINTER_VECTOR_RE.match(type0)
    if not match:
        return False
    width = match.group(1)
    atom_config = atom.get("config") or config
    expected_width = _pointer_bits(atom_config)
    if width is None or expected_width is None:
        return width in (None, "32", "64")
    return int(width) == expected_width


def classify_a3(corpus_doc, results, a2_probe=False, only_atom=None):
    """Classify operational anomalies into A3 known/new/coverage-gap buckets.

    ``expected`` is read here only; the C++ driver never sees it.  The fixed
    A2 probe is exempt: there ``failed|legalized`` is the expected answer.
    Otherwise A3 is met only when there are no coverage gaps and no non-family
    new findings; a budget/hang/crash outside the known BUILD_VECTOR family is
    a new finding and makes A3 "not met".
    """
    atoms = corpus_doc.get("atoms") or []
    config = corpus_config(corpus_doc)
    target = corpus_target(corpus_doc)
    by_id = {result.get("id"): result for result in results}
    known_anomalies = []
    new_findings = []
    coverage_gaps = []
    known_atoms = 0

    for atom in atoms:
        result = by_id.get(atom.get("id"))
        if result is None:
            continue
        # A ``--only-atom`` report is a single-atom probe, not corpus-level
        # A3 evidence: the other atoms are placeholders, not observations.
        if only_atom is not None and atom.get("id") != only_atom:
            continue
        outcome = result.get("outcome")
        if is_known_family(atom, config, target):
            known_atoms += 1
            detail_code = result.get("detail_code") or ""
            if a2_probe:
                if outcome in ANOMALY_OUTCOMES:
                    known_anomalies.append(result)
                continue
            if outcome in ("budget", "hang"):
                known_anomalies.append(result)
            elif outcome == "crash" and detail_code == "crash:construction":
                coverage_gaps.append(dict(result, a3_reason=detail_code))
            elif outcome == "crash":
                # Legalizer crash is real evidence, but not the expected
                # livelock; record it and make A3 not met.
                known_anomalies.append(result)
                coverage_gaps.append(
                    dict(result, a3_reason="known atom crashed: " + detail_code))
            else:
                coverage_gaps.append(
                    dict(result, a3_reason=f"known atom ended {outcome}"))
        elif outcome in ANOMALY_OUTCOMES:
            finding = dict(result)
            if outcome == "crash" and result.get("detail_code") == \
                    "crash:construction":
                finding["a3_reason"] = "non-family construction crash"
            new_findings.append(finding)

    gaps = len(coverage_gaps)
    findings = len(new_findings)
    exempt = bool(a2_probe or only_atom is not None)
    met = exempt or (gaps == 0 and findings == 0)
    return {
        "known_anomalies": known_anomalies,
        "new_findings": new_findings,
        "coverage_gaps": coverage_gaps,
        "a3": {
            "status": "met" if met else "not met",
            "met": met,
            "exempt": exempt,
            "known_atoms": known_atoms,
            "known_anomalies": len(known_anomalies),
            "new_findings": findings,
            "coverage_gaps": gaps,
        },
    }


# ---------------------------------------------------------------------------
# Report construction and exit contract


def _summary_reasons(summary):
    """Extract opcode -> reason pairs from a driver summary event."""
    reasons = {}
    if not isinstance(summary, dict):
        return reasons
    for key in ("unbuildable", "unbuildable_reasons"):
        value = summary.get(key)
        if isinstance(value, dict):
            for opcode, reason in value.items():
                if isinstance(reason, str):
                    reasons[opcode] = reason
                elif isinstance(reason, dict):
                    text = reason.get("reason") or reason.get("detail")
                    if isinstance(text, str):
                        reasons[opcode] = text
        elif isinstance(value, list):
            for item in value:
                if not isinstance(item, dict):
                    continue
                opcode = item.get("opcode")
                reason = item.get("reason") or item.get("detail")
                if opcode and isinstance(reason, str):
                    reasons[opcode] = reason
    opcodes = summary.get("opcodes")
    if isinstance(opcodes, dict):
        for opcode, info in opcodes.items():
            if not isinstance(info, dict):
                continue
            for key in ("unbuildable_reason", "unbuildable", "reason"):
                reason = info.get(key)
                if isinstance(reason, str):
                    reasons.setdefault(opcode, reason)
                    break
    return reasons


def build_report(corpus_doc, results, corpus_path, corpus_sha256, rules,
                 max_steps, timeout_s, shard_size, warnings=None,
                 summaries=None, only_atom=None, a2_probe=False):
    """Build (and validate) the ``llvm-x86-legalizer-mir-run`` report."""
    atoms = corpus_doc.get("atoms") or []
    if len(results) != len(atoms):
        raise MirRunInfraError(
            f"internal error: {len(results)} results for {len(atoms)} atoms")
    results = [dict(result) for result in results]

    reasons = {}
    for summary in summaries or ():
        for opcode, reason in _summary_reasons(summary).items():
            reasons.setdefault(opcode, reason)
    for result in results:
        if result.get("outcome") == "unbuildable" and not result.get("detail"):
            reason = reasons.get(result.get("opcode"))
            if reason:
                result["detail"] = reason

    counts = {key: 0 for key in COUNT_KEYS}
    for result in results:
        outcome = result.get("outcome")
        if outcome not in counts:
            raise MirRunInfraError(
                f"internal error: unclassified outcome {outcome!r}")
        counts[outcome] += 1
    if sum(counts.values()) != len(atoms):
        raise MirRunInfraError(
            "internal error: counts do not conserve the corpus atoms")

    # ``not_run`` entries are placeholders for atoms this invocation did not
    # observe (only-atom runs, or a stall reported elsewhere).  They are not
    # evidence, so they never become the reported first anomaly.
    first_anomaly = None
    for result in results:
        if result.get("outcome") in ("legalized", "failed", "not_run"):
            continue
        first_anomaly = result
        break

    a3 = classify_a3(corpus_doc, results, a2_probe=a2_probe,
                     only_atom=only_atom)
    report = {
        "schema": SCHEMA,
        "version": VERSION,
        "target": corpus_target(corpus_doc),
        "corpus": {
            "path": os.path.abspath(corpus_path),
            "sha256": corpus_sha256,
            "target": corpus_target(corpus_doc),
            "config": corpus_config(corpus_doc),
            "atoms": len(atoms),
        },
        "rules": rules,
        "budget": {
            "max_steps": max_steps,
            "timeout_s": timeout_s,
            "shard_size": shard_size,
        },
        "counts": counts,
        "first_anomaly": first_anomaly,
        "atoms": results,
        "anomalies": [result for result in results
                      if result.get("outcome") in ANOMALY_OUTCOMES],
        "new_findings": a3["new_findings"],
        "known_anomalies": a3["known_anomalies"],
        "coverage_gaps": a3["coverage_gaps"],
        "a3": a3["a3"],
        "libcalls": "disabled",
        "only_atom": only_atom,
        "infra_warnings": list(warnings or ()),
        "unbuildable_reasons": reasons,
        "shard_summaries": list(summaries or ()),
    }
    return report


def exit_code(report, only_atom=None):
    """Apply the rev 7 exit contract: 0 terminal, 1 anomaly, 2 infra."""
    atoms = report.get("atoms") or []
    if any(atom.get("outcome") in ANOMALY_OUTCOMES for atom in atoms):
        return 1
    if only_atom is None:
        only_atom = report.get("only_atom")
    if only_atom is not None:
        selected = [atom for atom in atoms if atom.get("id") == only_atom]
        if len(selected) != 1:
            return 2
        return 0 if selected[0].get("outcome") in TERMINAL_OUTCOMES else 2
    for atom in atoms:
        outcome = atom.get("outcome")
        if outcome in TERMINAL_OUTCOMES:
            continue
        if outcome == "not_run":
            reason = atom.get("reason") or ""
            if reason in FOLLOW_REASONS:
                expected = FOLLOW_REASONS[reason]
                if any(other.get("outcome") == expected for other in atoms):
                    continue
            return 2
        return 2
    return 0


# ---------------------------------------------------------------------------
# Running the driver


def partition_shards(atoms, shard_count):
    """Assign global atom positions to shards in one pass.

    Bucket ``n`` holds a contiguous ``(position, atom)`` slice.  Contiguous
    slices let the supervisor hand each driver a small corpus file instead of
    every driver re-parsing the whole corpus (a 1.7M-atom A2 full corpus costs
    ~19 GB of JSON DOM per process).  Empty buckets are retained so callers
    can derive shard indices by enumeration.
    """
    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    indexed = list(enumerate(atoms))
    size = max(1, math.ceil(len(indexed) / shard_count))
    buckets = [indexed[start:start + size]
               for start in range(0, len(indexed), size)]
    while len(buckets) < shard_count:
        buckets.append([])
    return buckets[:shard_count]


def _discard_placeholders_after_anomaly(results):
    """Drop ``not_run`` placeholders that follow an anomaly in one run.

    A resumed driver invocation owns the remaining atoms, so the classifier's
    placeholders for atoms after a budget/hang/crash are not evidence.  Earlier
    ``not_run`` results (before any anomaly) are kept.
    """
    kept = []
    anomaly_seen = False
    for result in results:
        outcome = result.get("outcome")
        if outcome in ANOMALY_OUTCOMES:
            anomaly_seen = True
        elif anomaly_seen and outcome == "not_run":
            continue
        kept.append(result)
    return kept


def _merge_invocation_results(merged, results):
    """Merge one invocation's results, never regressing to ``not_run``."""
    for result in results:
        index = result.get("index")
        previous = merged.get(index)
        if (previous is not None and result.get("outcome") == "not_run"
                and previous.get("outcome") != "not_run"):
            continue
        merged[index] = result


def run_shard_with_resume(binary, doc, tmp, out_path, rules, max_steps,
                          shard_index, shard_count, timeout, shard_atoms):
    """Run one shard, restarting after each anomaly while atoms remain.

    Each invocation gets a corpus file holding only the atoms still to run,
    plus ``X86_LEGALIZER_MIR_INDEX_BASE`` so the driver's markers keep the
    original global index.  After an invocation, results beyond the last
    non-``not_run`` atom are retried from that point.  Restarts are capped at
    ``len(shard_atoms) + 1``; if no atom advances, the remaining atoms stay
    ``not_run`` for an honest report instead of looping.
    """
    merged = {}
    warnings = []
    summaries = []
    max_restarts = len(shard_atoms) + 1
    restarts = 0
    start_index = 0
    last_progress = -1
    grace_timeout = False
    while True:
        remaining = [(pos, atom) for pos, atom in shard_atoms
                     if pos >= start_index]
        if not remaining:
            break
        shard_corpus = os.path.join(
            tmp, f"shard-{shard_index}-{restarts}.json")
        _write_shard_corpus(shard_corpus, doc, remaining)
        shard = _run_shard(
            binary, shard_corpus, out_path, rules, max_steps, 0,
            1, timeout * 6 if grace_timeout else timeout, remaining,
            start_index=0, index_base=remaining[0][0])
        warnings.extend(shard["warnings"])
        if shard["summary"] is not None:
            summaries.append(shard["summary"])
        _merge_invocation_results(
            merged, _discard_placeholders_after_anomaly(shard["results"]))

        resolved = [result["index"] for result in merged.values()
                    if result.get("outcome") != "not_run"]
        progress = max(resolved) if resolved else -1
        if progress <= last_progress:
            # A timeout with no marker at all is usually corpus parse/load
            # under contention, not evidence about an atom.  Give it one
            # longer attempt before leaving the atoms not_run.
            if not grace_timeout and any(
                    result.get("reason") == "timeout_before_start"
                    for result in shard["results"]):
                grace_timeout = True
                warnings.append(
                    "retrying shard after a pre-start timeout with a "
                    "longer deadline")
                continue
            break
        last_progress = progress
        # The 6x grace deadline is one-shot: once an invocation makes progress
        # again, later timeouts start from the normal deadline.
        grace_timeout = False
        if all(merged.get(pos, {}).get("outcome") not in (None, "not_run")
               for pos, _ in shard_atoms):
            break
        if restarts >= max_restarts:
            break
        restarts += 1
        start_index = progress + 1

    results = []
    for pos, atom in shard_atoms:
        result = merged.get(pos)
        if result is None:
            result = _not_run(atom, pos, "missing_result")
        results.append(result)
    return {"results": results, "warnings": warnings,
            "summaries": summaries}


def _run_shard(binary, corpus_path, out_path, rules, max_steps, shard_index,
               shard_count, timeout, shard_atoms, start_index=0,
               index_base=0):
    env = os.environ.copy()
    env.update({
        "X86_LEGALIZER_MIR_CORPUS_IN": os.path.abspath(corpus_path),
        "X86_LEGALIZER_MIR_OUT": os.path.abspath(out_path),
        "X86_LEGALIZER_MIR_MAX_STEPS": str(max_steps),
        "X86_LEGALIZER_MIR_SHARD": f"{shard_index}/{shard_count}",
        "X86_LEGALIZER_MIR_RULES": rules,
        "X86_LEGALIZER_MIR_START_INDEX": str(start_index),
        "X86_LEGALIZER_MIR_INDEX_BASE": str(index_base),
    })
    stderr_path = out_path + ".stderr"
    for path in (out_path, stderr_path):
        if os.path.exists(path):
            os.unlink(path)
    timed_out = False
    returncode = None
    command = driver_command(binary)
    try:
        with open(stderr_path, "w", encoding="utf-8") as stderr_handle:
            proc = subprocess.run(
                command, env=env, stdout=subprocess.DEVNULL,
                stderr=stderr_handle, text=True, timeout=timeout)
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
    except OSError as err:
        raise MirRunInfraError(f"cannot run driver {binary}: {err}") from err
    stderr = ""
    try:
        with open(stderr_path, encoding="utf-8") as stderr_handle:
            stderr = stderr_handle.read()
    except OSError:
        pass

    if returncode == 2 or INFRA_MARKER in stderr:
        raise MirRunInfraError(
            "driver infrastructure error: " + (stderr_tail(stderr) or
                                               f"exit {returncode}"))
    if not os.path.exists(out_path):
        if timed_out:
            # Killed before it opened the output: no atom was observed.  The
            # classifier reports not_run; the caller may retry with a grace
            # timeout.  This is not an infrastructure failure by itself.
            text = ""
        else:
            raise MirRunInfraError(
                "driver did not create its JSONL output "
                f"{out_path} (exit {returncode})")
    else:
        with open(out_path, encoding="utf-8") as handle:
            text = handle.read()
    events = parse_jsonl(text)
    return classify_shard(
        events, shard_atoms, timed_out=timed_out, returncode=returncode,
        stderr=stderr, max_steps=max_steps)


def run(corpus_path, out_path, build=None, driver=None, rules="production",
        shard_size=2000, timeout=120, max_steps=10000, only_atom=None,
        a2_probe=False, jobs=1, target=None):
    """Run the corpus through the driver, write the report, return it."""
    if rules not in ("production", "reference"):
        raise MirRunInfraError(f"invalid --rules {rules!r}")
    if shard_size < 1:
        raise MirRunInfraError("--shard-size must be at least 1")
    if jobs < 1:
        raise MirRunInfraError("--jobs must be at least 1")
    if timeout <= 0:
        raise MirRunInfraError("--timeout must be positive")
    if max_steps < 1:
        raise MirRunInfraError("--max-steps must be positive")

    corpus_path = os.path.abspath(corpus_path)
    doc = load_corpus(corpus_path)
    atoms, _ = validate_corpus(doc)
    corpus_tgt = corpus_target(doc)
    if target is not None and target != corpus_tgt:
        raise MirRunInfraError(
            f"--target {target!r} != corpus target {corpus_tgt!r}")
    if rules == "reference" and corpus_tgt != "x86":
        raise MirRunInfraError(
            "the frozen reference rules exist only for x86")
    target = corpus_tgt
    binary = resolve_driver(build=build, driver=driver)
    digest = sha256_file(corpus_path)

    warnings = []
    summaries = []
    results = []
    with tempfile.TemporaryDirectory(prefix="x86-mir-run-") as tmp:
        if only_atom is not None:
            positions = [pos for pos, atom in enumerate(atoms)
                         if atom.get("id") == only_atom]
            if len(positions) != 1:
                raise MirRunInfraError(
                    f"--only-atom {only_atom!r} matches {len(positions)} atoms")
            position = positions[0]
            temp_corpus = dict(doc)
            temp_corpus["atoms"] = [atoms[position]]
            temp_corpus["atom_count"] = 1
            temp_path = os.path.join(tmp, "only-atom.json")
            _write_json(temp_path, temp_corpus)
            shard = _run_shard(
                binary, temp_path, os.path.join(tmp, "only-atom.jsonl"),
                rules, max_steps, 0, 1, timeout,
                [(position, atoms[position])])
            warnings.extend(shard["warnings"])
            if shard["summary"] is not None:
                summaries.append(shard["summary"])
            selected = shard["results"][0]
            for pos, atom in enumerate(atoms):
                if pos == position:
                    results.append(selected)
                else:
                    results.append(_not_run(atom, pos, ONLY_ATOM_REASON))
        else:
            shard_count = (max(1, math.ceil(len(atoms) / shard_size))
                           if atoms else 1)
            specs = []
            for shard_index, shard_atoms in enumerate(
                    partition_shards(atoms, shard_count)):
                if not shard_atoms:
                    continue
                specs.append((
                    shard_index, shard_atoms,
                    os.path.join(tmp, f"shard-{shard_index}.jsonl")))

            def run_spec(spec):
                shard_index, shard_atoms, shard_out = spec
                return run_shard_with_resume(
                    binary, doc, tmp, shard_out, rules, max_steps,
                    shard_index, shard_count, timeout, shard_atoms)

            if jobs == 1 or len(specs) <= 1:
                shard_reports = [run_spec(spec) for spec in specs]
            else:
                with ThreadPoolExecutor(
                        max_workers=min(jobs, len(specs))) as executor:
                    # map preserves submission order, so warnings/summaries
                    # stay deterministic even though the shards overlap.
                    shard_reports = list(executor.map(run_spec, specs))

            by_index = {}
            for shard in shard_reports:
                warnings.extend(shard["warnings"])
                summaries.extend(shard["summaries"])
                for result in shard["results"]:
                    by_index[result["index"]] = result
            for pos, atom in enumerate(atoms):
                if pos not in by_index:
                    by_index[pos] = _not_run(atom, pos, "missing_result")
                results.append(by_index[pos])

    report = build_report(
        doc, results, corpus_path, digest, rules, max_steps, timeout,
        shard_size, warnings=warnings, summaries=summaries,
        only_atom=only_atom, a2_probe=a2_probe)
    _write_json(out_path, report)
    return report, exit_code(report, only_atom=only_atom)
