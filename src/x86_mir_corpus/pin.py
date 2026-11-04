"""The LLVM revision pin for the bundled X86 legalizer source snapshot.

The tool ships one C++ snapshot and must not silently follow ``upstream/main``.
``assets/llvm-pin.json`` records the exact LLVM commit that snapshot was taken
from, the snapshot's SHA-256, and the frozen reference revision.  ``verify``
re-checks the local artifacts and, when an LLVM checkout is given, re-extracts
the blob from that commit and compares hashes.
"""

import hashlib
import json
import os
import subprocess
from datetime import date

SCHEMA = "x86-mir-corpus-llvm-pin"
VERSION = 1
ASSET = "llvm-pin.json"
SOURCE_PATH = "llvm/lib/Target/X86/GISel/X86LegalizerInfo.cpp"
CHECK_SCHEMA = "x86-mir-corpus-llvm-pin-check"
FALLBACK_REVISION = "upstream/main"


def package_dir():
    """Directory of the installed ``x86_mir_corpus`` package."""
    return os.path.dirname(os.path.abspath(__file__))


def asset_path():
    """Absolute path of the pin file shipped inside the package."""
    return os.path.join(package_dir(), "assets", ASSET)


def repo_root():
    """Repository root when running from a checkout, else the package root."""
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))


def load(path=None):
    """Return the pin document, or None when it is missing or malformed."""
    path = path or asset_path()
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return None
    return doc


def revision(path=None):
    """The pinned LLVM revision, or None when no usable pin is present."""
    doc = load(path)
    value = (doc or {}).get("revision")
    return value if isinstance(value, str) and value else None


def default_source_rev(path=None):
    """Revision to use when the caller does not pass ``--source-rev``."""
    return revision(path) or FALLBACK_REVISION


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path):
    with open(path, "rb") as handle:
        return _sha256_bytes(handle.read())


def _git(root, *args, input_text=None):
    proc = subprocess.run(
        ["git", "-C", root, *args], input=input_text, capture_output=True,
        text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def _resolve_snapshot(snapshot):
    """Locate a snapshot from a checkout or an installed wheel.

    The pin stores a package-relative path (``assets/source-snapshots/...``);
    older pins used a repository-relative one (``src/x86_mir_corpus/...``).
    Both spellings, and a bare package lookup by file name, are accepted.
    """
    candidates = [
        os.path.join(package_dir(), snapshot),
        os.path.join(repo_root(), snapshot),
        os.path.join(package_dir(), "assets", "source-snapshots",
                     os.path.basename(snapshot)),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[0]


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def verify(repo=None, path=None):
    """Check the pin against the bundled snapshot and the repo metadata."""
    doc = load(path)
    report = {
        "schema": CHECK_SCHEMA,
        "version": VERSION,
        "ok": False,
        "pin": path or asset_path(),
        "revision": None,
        "snapshot": None,
        "checks": [],
        "reasons": [],
    }

    def check(name, ok, detail=None):
        entry = {"check": name, "ok": bool(ok)}
        if detail is not None:
            entry["detail"] = detail
        report["checks"].append(entry)
        if not ok:
            report["reasons"].append(name)
        return ok

    if doc is None:
        report["reasons"].append("pin file is missing or has an unknown schema")
        return report

    rev = doc.get("revision")
    snapshot = doc.get("snapshot")
    report["revision"] = rev
    report["snapshot"] = snapshot
    root = repo_root()
    snap_path = _resolve_snapshot(snapshot) if snapshot else None

    check("revision-present", bool(rev), rev)
    check("snapshot-present", bool(snap_path and os.path.exists(snap_path)),
          snapshot)
    if snap_path and os.path.exists(snap_path):
        digest = _sha256_file(snap_path)
        check("snapshot-sha256", digest == doc.get("snapshot_sha256"), digest)
        prefix = os.path.basename(snap_path).split("-")[0]
        check("snapshot-name", bool(rev) and rev.startswith(prefix), prefix)

    source_commit = _read_text(os.path.join(root, "SOURCE-COMMIT"))
    if source_commit is not None:
        recorded = ""
        for line in source_commit.splitlines():
            if line.startswith("default-source-revision:"):
                recorded = line.split(":", 1)[1].strip()
                break
        check("source-commit-file", bool(rev) and rev.startswith(recorded),
              recorded)

    provenance = load_provenance()
    if provenance is not None:
        frozen = provenance.get("source_commit") or ""
        pinned_ref = doc.get("reference_revision") or ""
        check("reference-revision",
              bool(frozen) and (frozen.startswith(pinned_ref)
                                or pinned_ref.startswith(frozen)),
              {"pinned": pinned_ref, "provenance": frozen})

    if repo:
        try:
            inside = _git(repo, "rev-parse", "--is-inside-work-tree").strip()
        except RuntimeError:
            inside = ""
        check("llvm-checkout", inside == "true", repo)
        if inside == "true":
            try:
                _git(repo, "cat-file", "-e", f"{rev}^{{commit}}")
                check("llvm-revision", True, rev)
            except (RuntimeError, TypeError):
                check("llvm-revision", False, rev)
            try:
                blob = _git(repo, "show", f"{rev}:{SOURCE_PATH}")
                check("llvm-blob", _sha256_bytes(blob.encode("utf-8"))
                      == doc.get("snapshot_sha256"))
            except (RuntimeError, TypeError):
                check("llvm-blob", False, SOURCE_PATH)

    report["ok"] = bool(report["checks"]) and all(
        entry["ok"] for entry in report["checks"])
    return report


def load_provenance(path=None):
    """The frozen reference provenance document, or None."""
    if path is None:
        candidates = [
            os.path.join(package_dir(), "assets",
                         "x86-legalizer-reference-provenance.json"),
            os.path.join(repo_root(), "sidecar", "reference",
                         "x86-legalizer-reference-provenance.json"),
        ]
        path = next((p for p in candidates if os.path.exists(p)), candidates[0])
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def update(repo, revision_ref=None, path=None, dry_run=False):
    """Re-pin to *revision_ref* from an LLVM checkout.

    Writes the extracted snapshot, the pin file, and the root ``SOURCE-COMMIT``
    metadata.  With ``dry_run`` nothing is written and the planned changes are
    returned for review.
    """
    if not repo:
        raise RuntimeError("an LLVM checkout is required (--repo)")
    root = repo_root()
    pin_path = path or asset_path()
    rev = _git(repo, "rev-parse", "--verify",
               f"{revision_ref or 'upstream/main'}^{{commit}}").strip()
    text = _git(repo, "show", f"{rev}:{SOURCE_PATH}")
    data = text.encode("utf-8")
    digest = _sha256_bytes(data)

    snapshot_dir = os.path.join(root, "src", "x86_mir_corpus", "assets",
                                "source-snapshots")
    snapshot_name = f"{rev[:8]}-X86LegalizerInfo.cpp"
    snapshot_path = os.path.join(snapshot_dir, snapshot_name)
    snapshot_rel = os.path.relpath(snapshot_path, root).replace(os.sep, "/")
    snapshot_pkg = f"assets/source-snapshots/{snapshot_name}"

    doc = load(pin_path) or {
        "schema": SCHEMA,
        "version": VERSION,
        "source_path": SOURCE_PATH,
        "reference_revision": (load_provenance() or {}).get("source_commit"),
    }
    previous = doc.get("revision")
    doc.update({
        "schema": SCHEMA,
        "version": VERSION,
        "revision": rev,
        "source_path": SOURCE_PATH,
        "snapshot": snapshot_pkg,
        "snapshot_sha256": digest,
        "recorded": date.today().isoformat(),
    })

    source_commit_path = os.path.join(root, "SOURCE-COMMIT")
    source_commit_text = _read_text(source_commit_path)
    new_source_commit = None
    if source_commit_text is not None:
        lines = []
        for line in source_commit_text.splitlines():
            if line.startswith("default-source-snapshot:"):
                line = f"default-source-snapshot: {snapshot_rel}"
            elif line.startswith("default-source-revision:"):
                line = f"default-source-revision: {rev[:8]}"
            lines.append(line)
        new_source_commit = "\n".join(lines) + "\n"

    changes = {
        "revision": rev,
        "previous_revision": previous,
        "snapshot": snapshot_pkg,
        "snapshot_sha256": digest,
        "pin": pin_path if path else os.path.relpath(pin_path, root),
        "source_commit_updated": new_source_commit is not None,
        "default_source_revision": rev[:8],
        "default_source_snapshot": snapshot_rel,
        "source_snapshot": snapshot_rel,
        "dry_run": bool(dry_run),
    }
    if dry_run:
        return changes

    os.makedirs(snapshot_dir, exist_ok=True)
    with open(snapshot_path, "wb") as handle:
        handle.write(data)
    with open(pin_path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=2, sort_keys=True)
        handle.write("\n")
    if new_source_commit is not None:
        with open(source_commit_path, "w", encoding="utf-8") as handle:
            handle.write(new_source_commit)
    return changes
