"""Tests for the bundled LLVM revision pin."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import testlib
from x86_mir_corpus import pin, target


class BundledPinTest(unittest.TestCase):
    def test_bundled_pin_verifies_locally(self):
        report = pin.verify()
        self.assertTrue(report["ok"], report["reasons"])
        self.assertTrue(report["revision"])

    def test_default_source_rev_is_the_pinned_commit(self):
        self.assertEqual(target.DEFAULT_SOURCE_REV, pin.revision())

    def test_snapshot_name_carries_the_revision_prefix(self):
        doc = pin.load()
        name = os.path.basename(doc["snapshot"])
        self.assertTrue(doc["revision"].startswith(name.split("-")[0]))

    def test_verify_reports_a_hash_mismatch(self):
        doc = pin.load()
        doc["snapshot_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "llvm-pin.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(doc, handle)
            report = pin.verify(path=path)
        self.assertFalse(report["ok"])
        self.assertIn("snapshot-sha256", report["reasons"])

    def test_verify_resolves_the_snapshot_from_an_installed_package(self):
        """Regression: the pin must not depend on the repository layout."""
        with tempfile.TemporaryDirectory() as tmp:
            site = os.path.join(tmp, "site-packages")
            shutil.copytree(os.path.dirname(pin.__file__),
                            os.path.join(site, "x86_mir_corpus"))
            env = dict(os.environ, PYTHONPATH=site)
            proc = subprocess.run(
                [sys.executable, "-c",
                 "import json; from x86_mir_corpus import pin; "
                 "print(json.dumps(pin.verify()))"],
                cwd=tmp, env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertTrue(report["ok"], report["reasons"])
        self.assertTrue(any(c["check"] == "snapshot-present" and c["ok"]
                            for c in report["checks"]))

    def test_cli_show_prints_the_pin(self):
        proc = testlib.run_cli("pin", "show", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertEqual(doc["revision"], pin.revision())


class UpdateTest(unittest.TestCase):
    def make_repo(self, tmp):
        path = os.path.join(tmp, "llvm-project")
        os.makedirs(os.path.join(path, "llvm", "lib", "Target", "X86", "GISel"))
        source = os.path.join(path, pin.SOURCE_PATH)
        with open(source, "w", encoding="utf-8") as handle:
            handle.write("// pinned source\n")
        env = ["git", "-C", path]
        subprocess.run([*env, "init", "-q"], check=True)
        subprocess.run([*env, "add", "-A"], check=True)
        subprocess.run(
            [*env, "-c", "user.name=Test", "-c", "user.email=test@example.com",
             "commit", "-q", "-m", "snapshot"], check=True)
        sha = subprocess.run([*env, "rev-parse", "HEAD"], check=True,
                             capture_output=True, text=True).stdout.strip()
        return path, sha

    def test_dry_run_reports_the_planned_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, sha = self.make_repo(tmp)
            before = pin.load()
            changes = pin.update(repo, "HEAD", dry_run=True)
            after = pin.load()
        self.assertEqual(changes["revision"], sha)
        self.assertEqual(changes["previous_revision"], before["revision"])
        self.assertEqual(changes["snapshot_sha256"],
                         pin._sha256_bytes(b"// pinned source\n"))
        self.assertTrue(changes["snapshot"].endswith(
            f"{sha[:8]}-X86LegalizerInfo.cpp"))
        self.assertEqual(changes["default_source_revision"], sha[:8])
        self.assertEqual(changes["snapshot"],
                         f"assets/source-snapshots/{sha[:8]}-"
                         "X86LegalizerInfo.cpp")
        self.assertEqual(changes["source_snapshot"],
                         f"src/x86_mir_corpus/{changes['snapshot']}")
        self.assertEqual(before, after, "dry-run must not write the pin")

    def test_update_requires_a_repo(self):
        with self.assertRaises(RuntimeError):
            pin.update(None, "HEAD", dry_run=True)


if __name__ == "__main__":
    unittest.main()


class AArch64PinTest(unittest.TestCase):
    def test_bundled_aarch64_pin_verifies_locally(self):
        report = pin.verify(target="aarch64")
        self.assertTrue(report["ok"], report["reasons"])
        self.assertEqual(report["target"], "aarch64")

    def test_aarch64_source_path_and_snapshot(self):
        doc = pin.load(target="aarch64")
        self.assertEqual(
            doc["source_path"],
            "llvm/lib/Target/AArch64/GISel/AArch64LegalizerInfo.cpp")
        self.assertIn("AArch64LegalizerInfo.cpp", doc["snapshot"])
        self.assertIsNone(doc.get("reference_revision"))

    def test_cli_show_prints_the_aarch64_pin(self):
        proc = testlib.run_cli("pin", "show", "--target", "aarch64", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertEqual(doc["target"], "aarch64")
