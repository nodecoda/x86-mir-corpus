"""Discovery, doctor checks, and out-of-tree sidecar builds.

Path resolution order is CLI flag, environment variable, then the repository
default:

    X86_MIR_LLVM_SRC        llvm-project checkout
    X86_MIR_LLVM_BUILD      LLVM build directory (contains LLVMConfig.cmake)
    X86_MIR_SIDECAR_BUILD   directory containing x86-mir-{oracle,run}
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

from . import __version__
from . import schemas

DOCTOR_SCHEMA = "x86-mir-corpus-doctor"
DOCTOR_VERSION = 1
SIDECAR_BUILD_SCHEMA = "x86-mir-corpus-sidecar-build"
SIDECAR_BUILD_VERSION = 1

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(PACKAGE_DIR))
PROVENANCE_PATH = os.path.join(PACKAGE_DIR, "assets",
                               "x86-legalizer-reference-provenance.json")
SOURCE_PATH = "llvm/lib/Target/X86/GISel/X86LegalizerInfo.cpp"


def _real(path):
    return os.path.realpath(os.path.abspath(path)) if path else None


def llvm_source_dir(path):
    """The ``llvm/`` source directory inside an llvm-project checkout."""
    path = _real(path)
    if path is None:
        return None
    nested = os.path.join(path, "llvm")
    if os.path.isdir(os.path.join(nested, "lib", "Target")):
        return nested
    return path


def load_config(path=None):
    """Load a JSON path configuration, or an empty dict when absent.

    Order: explicit path, $X86_MIR_CONFIG, user config, then
    ./x86-mir-corpus.json.
    """
    candidates = [path, os.environ.get("X86_MIR_CONFIG"),
                  os.path.join(os.path.expanduser("~"), ".config",
                               "x86-mir-corpus", "config.json"),
                  os.path.join(os.getcwd(), "x86-mir-corpus.json")]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        return data if isinstance(data, dict) else {}
    return {}


def find_paths(llvm_src=None, llvm_build=None, sidecar_build=None):
    config = load_config()
    src = _real(llvm_src or os.environ.get("X86_MIR_LLVM_SRC")
                or config.get("llvm_src"))
    build = _real(llvm_build or os.environ.get("X86_MIR_LLVM_BUILD")
                  or config.get("llvm_build"))
    sidecar = _real(sidecar_build or os.environ.get("X86_MIR_SIDECAR_BUILD")
                    or config.get("sidecar_build")
                    or os.path.join(os.getcwd(), ".build"))
    return src, build, sidecar


def _git(root, *args):
    proc = subprocess.run(["git", "-C", root, *args],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True)
    if proc.returncode != 0:
        return None
    return proc.stdout


def git_revision(llvm_src):
    out = _git(llvm_src, "rev-parse", "HEAD")
    return out.strip() if out else None


def _cmake_var(text, name):
    match = re.search(rf"(?m)^set\({name}\s+(.*)\)\s*$", text)
    return match.group(1).strip().strip('"') if match else None


def llvm_build_info(build):
    config = os.path.join(build, "lib", "cmake", "llvm", "LLVMConfig.cmake")
    try:
        with open(config, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    targets = (_cmake_var(text, "LLVM_TARGETS_TO_BUILD") or "").strip('"')
    return {
        "config": config,
        "version": _cmake_var(text, "LLVM_PACKAGE_VERSION"),
        "assertions": (_cmake_var(text, "LLVM_ENABLE_ASSERTIONS") or "").upper()
        in ("ON", "TRUE", "1"),
        "targets": [t for t in targets.split(";") if t],
        "source_dir": _cmake_var(text, "LLVM_BUILD_MAIN_SRC_DIR"),
        "include_dirs": (_cmake_var(text, "LLVM_INCLUDE_DIRS") or "").split(";"),
    }


def sidecar_binaries(build):
    found = {}
    for name in ("x86-mir-oracle", "x86-mir-run", "x86-mir-smoke"):
        path = os.path.join(build, name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            found[name] = path
    return found


def sidecar_build_info(build):
    path = os.path.join(build, "sidecar-build-info.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _sidecar_source_dir():
    env = os.environ.get("X86_MIR_SIDECAR_SRC")
    if env:
        return _real(env)
    for candidate in (os.path.join(REPO_ROOT, "sidecar"),
                      os.path.join(os.getcwd(), "sidecar")):
        if os.path.isfile(os.path.join(candidate, "CMakeLists.txt")):
            return candidate
    return None


def supported_revisions():
    path = os.path.join(PACKAGE_DIR, "assets", "supported-revisions.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle).get("revisions", [])
    except (OSError, ValueError):
        return []


def read_provenance(path=PROVENANCE_PATH):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def blob_sha256(llvm_src, revision, source_path=SOURCE_PATH):
    text = _git(llvm_src, "show", f"{revision}:{source_path}")
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_provenance(llvm_src, provenance=None):
    """Check the frozen reference against the LLVM checkout.

    Returns a dict with ``ok`` and ``reasons``; a missing checkout is a
    reason, not an exception.
    """
    provenance = provenance or read_provenance()
    reasons = []
    result = {
        "path": PROVENANCE_PATH,
        "source_commit": None,
        "source_blob": None,
        "builder_count": None,
        "present": bool(provenance),
    }
    frozen = os.path.join(REPO_ROOT, "sidecar", "reference",
                          "X86LegalizerInfoReference.cpp")
    if not os.path.exists(frozen):
        sidecar = _sidecar_source_dir()
        frozen = os.path.join(sidecar, "reference",
                              "X86LegalizerInfoReference.cpp") \
            if sidecar else frozen
    try:
        with open(frozen, "r", encoding="utf-8") as handle:
            result["frozen_builders"] = handle.read().count(
                "getActionDefinitionsBuilder")
    except OSError:
        result["frozen_builders"] = None
    if not provenance:
        reasons.append("reference provenance is missing")
        result["ok"] = False
        result["reasons"] = reasons
        return result
    result["source_commit"] = provenance.get("source_commit")
    result["source_blob"] = provenance.get("source_blob")
    result["builder_count"] = provenance.get("builder_count")
    if provenance.get("schema") != schemas.PROVENANCE_SCHEMA:
        reasons.append("unexpected provenance schema")
    if llvm_src is None:
        reasons.append("no LLVM source checkout to verify provenance")
    else:
        commit = provenance.get("source_commit")
        if not commit:
            reasons.append("provenance has no source_commit")
        elif _git(llvm_src, "cat-file", "-e", f"{commit}^{{commit}}") is None:
            reasons.append(f"provenance commit {commit} is not in the checkout")
        else:
            actual = blob_sha256(llvm_src, commit)
            expected = provenance.get("source_blob")
            result["git_blob_sha256"] = actual
            if actual != expected:
                reasons.append("provenance source_blob does not match git show")
        builders = result["frozen_builders"]
        if builders is not None and provenance.get("builder_count") is not None \
                and builders != provenance["builder_count"]:
            reasons.append(
                f"frozen reference has {builders} builders, provenance records "
                f"{provenance['builder_count']}")
    result["ok"] = not reasons
    result["reasons"] = reasons
    return result


def doctor(llvm_src=None, llvm_build=None, sidecar_build=None,
           allow_unpinned=False):
    src, build, sidecar = find_paths(llvm_src, llvm_build, sidecar_build)
    reasons = []
    report = {
        "schema": DOCTOR_SCHEMA,
        "version": DOCTOR_VERSION,
        "package_version": __version__,
        "python": {
            "version": sys.version.split()[0],
            "executable": sys.executable,
            "ok": sys.version_info >= (3, 10),
        },
    }
    if not report["python"]["ok"]:
        reasons.append("Python >= 3.10 is required")

    report["llvm_src"] = {"path": src, "ok": False}
    if src is None or not os.path.isdir(os.path.join(src, "llvm")):
        reasons.append("LLVM source checkout not found (set X86_MIR_LLVM_SRC)")
    else:
        revision = git_revision(src)
        report["llvm_src"].update({
            "ok": True,
            "revision": revision,
        })
        supported = supported_revisions()
        if revision and supported and revision not in supported \
                and not allow_unpinned:
            reasons.append(
                f"unpinned LLVM revision {revision[:12]}; supported: "
                + ", ".join(item[:12] for item in supported)
                + " (pass --allow-unpinned for an experimental build)")

    info = llvm_build_info(build) if build else None
    report["llvm_build"] = {"path": build, "ok": False}
    if info is None:
        reasons.append("LLVM build not found (set X86_MIR_LLVM_BUILD)")
    else:
        report["llvm_build"].update({
            "ok": True,
            "version": info["version"],
            "assertions": info["assertions"],
            "targets": info["targets"],
        })
        if not info["assertions"]:
            reasons.append("LLVM build has assertions disabled")
        if "X86" not in info["targets"]:
            reasons.append("LLVM build does not include the X86 target")

    binaries = sidecar_binaries(sidecar)
    build_info = sidecar_build_info(sidecar)
    report["sidecar"] = {
        "build": sidecar,
        "binaries": binaries,
        "build_info": build_info,
        "ok": False,
    }
    missing = [name for name in ("x86-mir-oracle", "x86-mir-run")
               if name not in binaries]
    if missing:
        reasons.append("sidecar binaries missing: " + ", ".join(missing))
    else:
        report["sidecar"]["ok"] = True
    if build_info is None:
        reasons.append("sidecar build info is missing")
    else:
        if build_info.get("schema") != SIDECAR_BUILD_SCHEMA:
            reasons.append("sidecar build info has an unexpected schema")
        if info is not None and build_info.get("llvm_version") != info["version"]:
            reasons.append("sidecar and LLVM build versions differ")
        if build_info.get("llvm_source_dir") and src:
            if _real(build_info["llvm_source_dir"]) != llvm_source_dir(src):
                reasons.append("sidecar was built from a different LLVM source")

    report["provenance"] = check_provenance(src)
    if not report["provenance"]["ok"]:
        reasons.extend(report["provenance"]["reasons"])

    report["schemas"] = {
        "corpus": f"{schemas.CORPUS_SCHEMA} v{schemas.CORPUS_VERSION}",
        "mir_run": f"{schemas.MIR_RUN_SCHEMA} v{schemas.MIR_RUN_VERSION}",
        "baseline": f"{schemas.BASELINE_SCHEMA} v{schemas.BASELINE_VERSION}",
        "provenance": (f"{schemas.PROVENANCE_SCHEMA} "
                       f"v{schemas.PROVENANCE_VERSION}"),
    }
    report["reasons"] = reasons
    report["ok"] = not reasons
    return report


def build_sidecar(llvm_build, out, llvm_src=None, generator="Ninja",
                  debug=False, extra_args=()):
    """Configure and build the two sidecar drivers; return their paths."""
    build = _real(llvm_build)
    if build is None:
        raise SystemExit("llvm build: pass --llvm-build")
    llvm_dir = os.path.join(build, "lib", "cmake", "llvm")
    if not os.path.isfile(os.path.join(llvm_dir, "LLVMConfig.cmake")):
        raise SystemExit(f"llvm build: no LLVMConfig.cmake under {llvm_dir}")
    cmake = shutil.which("cmake")
    if not cmake:
        raise SystemExit("llvm build: cmake is not on PATH")
    source = _sidecar_source_dir()
    if source is None:
        raise SystemExit(
            "llvm build: sidecar sources not found; run from a source checkout "
            "or set X86_MIR_SIDECAR_SRC")
    out = _real(out)
    command = [
        cmake, "-S", source, "-B", out, "-G", generator,
        f"-DLLVM_DIR={llvm_dir}",
        "-DCMAKE_BUILD_TYPE=" + ("Debug" if debug else "Release"),
    ]
    if llvm_src:
        x86 = os.path.join(_real(llvm_src), "llvm", "lib", "Target", "X86")
        command.append(f"-DX86_SOURCE_DIR={x86}")
    command.extend(extra_args)
    proc = subprocess.run(command)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)
    proc = subprocess.run(
        [cmake, "--build", out, "--target", "x86-mir-oracle", "x86-mir-run"])
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)
    return sidecar_binaries(out)
