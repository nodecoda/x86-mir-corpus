"""Unit tests for LLVM discovery and doctor checks (no LLVM required)."""

import json
import os
import tempfile
import unittest
from unittest import mock

from x86_mir_corpus import llvm, schemas


class BuildInfoTest(unittest.TestCase):
    def make_build(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "lib", "cmake", "llvm")
        os.makedirs(path)
        with open(os.path.join(path, "LLVMConfig.cmake"), "w",
                  encoding="utf-8") as handle:
            handle.write(text)
        return tmp.name

    def test_parses_version_assertions_targets_and_source(self):
        build = self.make_build("""\
set(LLVM_PACKAGE_VERSION "24.0.0git")
set(LLVM_ENABLE_ASSERTIONS ON)
set(LLVM_TARGETS_TO_BUILD X86;AArch64)
set(LLVM_BUILD_MAIN_SRC_DIR "/src/llvm")
set(LLVM_INCLUDE_DIRS "/src/llvm/include;/build/include")
""")
        info = llvm.llvm_build_info(build)
        self.assertEqual(info["version"], "24.0.0git")
        self.assertTrue(info["assertions"])
        self.assertEqual(info["targets"], ["X86", "AArch64"])
        self.assertEqual(info["source_dir"], "/src/llvm")

    def test_missing_config_is_none(self):
        self.assertIsNone(llvm.llvm_build_info("/nonexistent/build"))


class PathDiscoveryTest(unittest.TestCase):
    def test_cli_paths_win_over_environment(self):
        with mock.patch.dict(os.environ, {
                "X86_MIR_LLVM_SRC": "/env/src",
                "X86_MIR_LLVM_BUILD": "/env/build",
                "X86_MIR_SIDECAR_BUILD": "/env/sidecar"}):
            src, build, sidecar = llvm.find_paths(
                "/cli/src", "/cli/build", "/cli/sidecar")
        self.assertEqual(src, "/cli/src")
        self.assertEqual(build, "/cli/build")
        self.assertEqual(sidecar, "/cli/sidecar")

    def test_environment_is_used_when_cli_is_absent(self):
        with mock.patch.dict(os.environ, {
                "X86_MIR_LLVM_SRC": "/env/src",
                "X86_MIR_LLVM_BUILD": "/env/build",
                "X86_MIR_SIDECAR_BUILD": "/env/sidecar"}):
            src, build, sidecar = llvm.find_paths()
        self.assertEqual(src, "/env/src")
        self.assertEqual(build, "/env/build")
        self.assertEqual(sidecar, "/env/sidecar")


class DoctorTest(unittest.TestCase):
    def test_missing_paths_are_reported_not_raised(self):
        report = llvm.doctor("/nonexistent/src", "/nonexistent/build",
                             "/nonexistent/sidecar")
        self.assertFalse(report["ok"])
        joined = " ".join(report["reasons"])
        self.assertIn("LLVM source checkout not found", joined)
        self.assertIn("LLVM build not found", joined)
        self.assertIn("sidecar binaries missing", joined)
        self.assertEqual(report["schema"], llvm.DOCTOR_SCHEMA)
        self.assertIn(schemas.CORPUS_SCHEMA, report["schemas"]["corpus"])

    def test_bundled_provenance_is_well_formed(self):
        provenance = llvm.read_provenance()
        self.assertIsNotNone(provenance)
        self.assertEqual(provenance["schema"], schemas.PROVENANCE_SCHEMA)
        self.assertEqual(provenance["version"],
                         schemas.PROVENANCE_VERSION)
        self.assertTrue(provenance["source_commit"])

    def test_frozen_reference_builder_count_matches_provenance(self):
        report = llvm.check_provenance(None)
        provenance = llvm.read_provenance()
        self.assertEqual(report["frozen_builders"],
                         provenance["builder_count"])
        self.assertFalse(report["ok"])
        self.assertIn("no LLVM source checkout", " ".join(report["reasons"]))


if __name__ == "__main__":
    unittest.main()
