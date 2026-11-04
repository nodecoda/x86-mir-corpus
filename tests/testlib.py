"""Shared helpers for the corpus-reader unit tests.

The tests run without an LLVM checkout.  Tests that need LLVM sources or the
committed witness skip unless ``X86_MIR_LLVM_SRC`` points at an llvm-project
checkout (or a checkout is vendored under ``<repo>/llvm``).
"""

import json
import os
import subprocess
import sys

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(TOOL_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from x86_mir_corpus import parse  # noqa: E402

SOURCE_PATH = "llvm/lib/Target/X86/GISel/X86LegalizerInfo.cpp"
WITNESS_PATH = "llvm/unittests/Target/X86/x86-legalizer-witness.json"


def repo_root():
    """The root of this tool's own repository."""
    return TOOL_DIR


def llvm_root():
    """An llvm-project checkout, or None when the tests must skip."""
    root = os.environ.get("X86_MIR_LLVM_SRC")
    if root:
        root = os.path.abspath(root)
        return root if os.path.isdir(os.path.join(root, "llvm")) else None
    vendored = os.path.join(TOOL_DIR, "llvm")
    if os.path.isdir(os.path.join(vendored, "llvm")):
        return vendored
    return None


def llvm_build():
    build = os.environ.get("X86_MIR_LLVM_BUILD")
    if build:
        build = os.path.abspath(build)
        return build if os.path.isdir(build) else None
    return None


def _provenance_path():
    return os.path.join(TOOL_DIR, "sidecar", "reference",
                        "x86-legalizer-reference-provenance.json")


def reference_revision():
    """The commit the frozen C++ reference was captured from."""
    try:
        with open(_provenance_path(), "r", encoding="utf-8") as handle:
            return json.load(handle)["source_commit"]
    except (OSError, KeyError, ValueError):
        return None


def _git_show(root, revision):
    proc = subprocess.run(
        ["git", "-C", root, "show", f"{revision}:{SOURCE_PATH}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        return None
    return proc.stdout


def reference_source():
    """Return (source_text, source_info) for the frozen reference revision.

    Returns None when no LLVM checkout or revision is available, so the census
    test skips instead of failing on a shallow clone.
    """
    root = llvm_root()
    revision = reference_revision()
    if root is None or revision is None:
        return None
    text = _git_show(root, revision)
    if text is None:
        return None
    return text, {
        "rev": revision,
        "path": SOURCE_PATH,
        "sha256": "test",
        "lines": text.count("\n") + 1,
    }


def source_at(revision):
    """Return (source_text, source_info) for *revision*, or None if absent."""
    root = llvm_root()
    if root is None or revision is None:
        return None
    text = _git_show(root, revision)
    if text is None:
        return None
    return text, {
        "rev": revision,
        "revision": revision,
        "path": SOURCE_PATH,
        "sha256": "test",
        "lines": text.count("\n") + 1,
    }


def witness_path():
    """The committed v1 witness used by migration/corpusdiff tests."""
    fixture = os.path.join(TOOL_DIR, "tests", "fixtures",
                           "x86-legalizer-witness.json")
    if os.path.exists(fixture):
        return fixture
    root = llvm_root()
    if root is not None:
        path = os.path.join(root, WITNESS_PATH)
        if os.path.exists(path):
            return path
    return None


def cli_argv(*args):
    return [sys.executable, "-m", "x86_mir_corpus", *args]


def cli_env(**extra):
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = SRC_DIR if not existing else SRC_DIR + os.pathsep + existing
    env.update({key: str(value) for key, value in extra.items()})
    return env


def cli_cwd():
    return llvm_root() or repo_root()


def run_cli(*args, env=None, cwd=None):
    env = dict(env) if env is not None else os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = SRC_DIR if not existing else SRC_DIR + os.pathsep + existing
    return subprocess.run(
        cli_argv(*args),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=env, cwd=cwd or cli_cwd())


def parse_text(text, config="C2"):
    from x86_mir_corpus.target import CONFIGS
    source = {"rev": None, "path": "<test>", "sha256": "test",
              "lines": text.count("\n") + 1}
    return parse.parse_source(text, source, config, CONFIGS[config])


def sidecar_binary(name="x86-mir-run"):
    """A sidecar binary from X86_MIR_SIDECAR_BUILD or <repo>/.build."""
    build = os.environ.get("X86_MIR_SIDECAR_BUILD")
    if not build:
        build = os.path.join(TOOL_DIR, ".build")
    path = os.path.join(build, name)
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    return None


def in_tree_driver():
    """The legacy in-tree X86Tests binary, or None."""
    build = llvm_build()
    if build is None:
        return None
    for rel in ("unittests/Target/X86/X86Tests", "bin/X86Tests"):
        path = os.path.join(build, rel)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def corpus_dir():
    return os.environ.get("X86_MIR_CORPUS_DIR")
