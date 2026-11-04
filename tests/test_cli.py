import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import testlib
from x86_mir_corpus import cli, oracle

MINI = """\
X86LegalizerInfo::X86LegalizerInfo(const X86Subtarget &STI) : Subtarget(STI) {
  const LLT s8 = LLT::scalar(8);
  const LLT s32 = LLT::scalar(32);
  getActionDefinitionsBuilder(G_ADD)
      .legalFor({{s32, s32}})
      .frobnicate({s8});
}
"""

TD_INC = """\
X86Tables[] = {
  /*0*/ {"X86_ADD", "G_ADD", {0}, {24}, false, 0, 3, 1}, // X86_ADD
};
"""

UNPROBED = """\
X86LegalizerInfo::X86LegalizerInfo(const X86Subtarget &STI) : Subtarget(STI) {
  const LLT s32 = LLT::scalar(32);
  getActionDefinitionsBuilder(G_ADD)
      .customIf([](const LegalityQuery &Q) { return Q.Types[0] != Q.Types[1]; });
}
"""


def run_cli(*args):
    return testlib.run_cli(*args)


class VersionFlagTest(unittest.TestCase):
    def test_version_flag_prints_the_package_version(self):
        from x86_mir_corpus import __version__

        proc = run_cli("--version")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(__version__, proc.stdout)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = os.path.join(self.tmp.name, "mini.cpp")
        with open(self.src, "w", encoding="utf-8") as handle:
            handle.write(MINI)

    def out(self, name):
        return os.path.join(self.tmp.name, name)

    def test_unknown_method_fails_with_line(self):
        proc = run_cli("parse", "--input", self.src, "--out", self.out("a.json"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unknown chain method frobnicate at line 6", proc.stderr)
        self.assertFalse(os.path.exists(self.out("a.json")))

    def test_allow_opaque_keeps_going(self):
        proc = run_cli("parse", "--input", self.src, "--allow-opaque",
                       "--out", self.out("a.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(self.out("a.json"), encoding="utf-8") as handle:
            doc = json.load(handle)
        self.assertEqual(doc["census"]["builders"], 1)
        self.assertEqual(doc["unknown_methods"][0]["method"], "frobnicate")

    def test_points_are_byte_deterministic(self):
        source = MINI.replace("      .frobnicate({s8});\n", "")
        with open(self.src, "w", encoding="utf-8") as handle:
            handle.write(source)
        blobs = []
        for name in ("p1.json", "p2.json"):
            proc = run_cli("points", "--input", self.src,
                           "--allow-coverage-gaps", "--out", self.out(name))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(self.out(name), "rb") as handle:
                blobs.append(handle.read())
        self.assertEqual(blobs[0], blobs[1])
        doc = json.loads(blobs[0])
        self.assertEqual(doc["tier"], "witness")
        self.assertEqual(doc["point_count"], len(doc["points"]))
        self.assertTrue(all("expected" not in p for p in doc["points"]))

    def test_corpus_without_build_writes_only_points(self):
        source = MINI.replace("      .frobnicate({s8});\n", "")
        with open(self.src, "w", encoding="utf-8") as handle:
            handle.write(source)
        proc = run_cli("corpus", "--input", self.src,
                       "--allow-coverage-gaps", "--out", self.out("corpus.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("pass --build", proc.stderr)
        self.assertTrue(os.path.exists(self.out("corpus.json.points.json")))
        self.assertFalse(os.path.exists(self.out("corpus.json")))

    def test_verify_rejects_non_witness_config(self):
        proc = run_cli("parse", "--input", self.src, "--allow-opaque",
                       "--config", "C0", "--out", self.out("c0.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        inc = self.out("rules.inc")
        with open(inc, "w", encoding="utf-8") as handle:
            handle.write(TD_INC)
        proc = run_cli("verify", "--ast", self.out("c0.json"),
                       "--td-inc", inc, "--out", self.out("rep.json"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--config C2", proc.stderr)

    def test_verify_needs_an_explicit_delta_registration(self):
        source = MINI.replace(
            "      .frobnicate({s8});",
            "      .legalIf([](unsigned, LLT, const LegalityQuery &Q) "
            "{ return typeIs(0, s32); });")
        with open(self.src, "w", encoding="utf-8") as handle:
            handle.write(source)
        proc = run_cli("parse", "--input", self.src, "--out", self.out("ast.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        inc = self.out("rules.inc")
        with open(inc, "w", encoding="utf-8") as handle:
            handle.write(TD_INC)

        proc = run_cli("verify", "--ast", self.out("ast.json"),
                       "--td-inc", inc, "--out", self.out("rep1.json"))
        self.assertEqual(proc.returncode, 3, proc.stderr)

        deltas = self.out("deltas.json")
        with open(deltas, "w", encoding="utf-8") as handle:
            json.dump({"deltas": [{"rule_set": "X86_ADD",
                                   "class": "td_expands_lambda"}]}, handle)
        proc = run_cli("verify", "--ast", self.out("ast.json"),
                       "--td-inc", inc, "--known-deltas", deltas,
                       "--out", self.out("rep2.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(self.out("rep2.json"), encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertEqual(report["unexplained"], [])
        self.assertEqual(report["known_deltas_used"],
                         ["X86_ADD:td_expands_lambda"])

    def test_oracle_none_emits_shapes_only(self):
        source = MINI.replace("      .frobnicate({s8});\n", "")
        with open(self.src, "w", encoding="utf-8") as handle:
            handle.write(source)
        proc = run_cli("corpus", "--input", self.src, "--oracle=none",
                       "--allow-coverage-gaps", "--out", self.out("shapes.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(self.out("shapes.json"), encoding="utf-8") as handle:
            doc = json.load(handle)
        self.assertEqual(doc["oracle"]["kind"], "none")
        self.assertFalse(doc["oracle"]["cross_revision"])
        self.assertEqual(doc["atom_count"], len(doc["atoms"]))
        self.assertTrue(doc["atoms"])
        self.assertTrue(all("expected" not in atom for atom in doc["atoms"]))


def fake_reference_fill(build, points_path, out_path):
    with open(points_path, encoding="utf-8") as handle:
        doc = json.load(handle)
    doc["schema"] = cli.CORPUS_SCHEMA
    doc["atoms"] = doc.pop("points")
    doc["atom_count"] = len(doc["atoms"])
    for atom in doc["atoms"]:
        atom["expected"] = {"action": "legal"}
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle)


class CrossRevisionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = os.path.join(self.tmp.name, "mini.cpp")
        with open(self.src, "w", encoding="utf-8") as handle:
            handle.write(MINI.replace("      .frobnicate({s8});\n", ""))

    def out(self, name):
        return os.path.join(self.tmp.name, name)

    def corpus_args(self, out, *extra):
        return ["corpus", "--input", self.src, "--oracle=reference",
                "--allow-coverage-gaps", "--build",
                os.path.join(self.tmp.name, "build"), "--out", out, *extra]

    def patch_oracle(self):
        stack = []
        stack.append(mock.patch.object(
            oracle, "reference_revision", return_value="oracle-rev"))
        stack.append(mock.patch.object(
            cli, "_cross_revision_delta",
            return_value={"source_only": [], "oracle_only": []}))
        for item in stack:
            item.start()
            self.addCleanup(item.stop)

    def test_reference_oracle_refuses_cross_revision(self):
        self.patch_oracle()
        out = os.path.join(self.tmp.name, "corpus.json")
        with self.assertRaises(SystemExit) as caught:
            cli.main(self.corpus_args(out))
        self.assertIn("cross-revision corpus", str(caught.exception))
        self.assertFalse(os.path.exists(out))

    def test_allow_cross_revision_marks_the_corpus(self):
        self.patch_oracle()
        self.addCleanup(mock.patch.stopall)
        with mock.patch.object(oracle, "run_reference_filler",
                               side_effect=fake_reference_fill):
            out = os.path.join(self.tmp.name, "corpus.json")
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(self.corpus_args(out, "--allow-cross-revision"))
        with open(out, encoding="utf-8") as handle:
            doc = json.load(handle)
        self.assertEqual(doc["oracle"]["kind"], "reference")
        self.assertTrue(doc["oracle"]["cross_revision"])
        self.assertEqual(doc["atom_count"], len(doc["atoms"]))
        self.assertTrue(all("expected" in atom for atom in doc["atoms"]))
        self.assertTrue(os.path.exists(out + ".report.json"))


    def test_unprobed_rule_fails_without_allow_coverage_gaps(self):
        path = os.path.join(self.tmp.name, "unprobed.cpp")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(UNPROBED)
        proc = run_cli("points", "--input", path, "--out", self.out("p.json"))
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("coverage gap: X86_ADD rule 0 customIf", proc.stderr)
        self.assertFalse(os.path.exists(self.out("p.json")))

        proc = run_cli("points", "--input", path, "--allow-coverage-gaps",
                       "--out", self.out("p2.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(self.out("p2.json"), encoding="utf-8") as handle:
            doc = json.load(handle)
        self.assertEqual(len(doc["coverage"]["rules"]["unprobed"]), 1)
        self.assertEqual(doc["coverage"]["rules"]["unprobed"][0]["rule_set"],
                         "X86_ADD")

    def test_symbolic_revision_is_resolved_in_the_manifest(self):
        root = testlib.llvm_root()
        if root is None:
            self.skipTest("X86_MIR_LLVM_SRC is not set")
        proc = subprocess.run(
            ["git", "-C", root, "rev-parse", "--verify",
             "upstream/main^{commit}"],
            capture_output=True, text=True)
        if proc.returncode != 0:
            self.skipTest("upstream/main is not available")
        expected = proc.stdout.strip()
        out = self.out("upstream.json")
        proc = run_cli("parse", "--source-rev", "upstream/main", "--out", out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(out, encoding="utf-8") as handle:
            doc = json.load(handle)
        self.assertEqual(doc["source"]["rev"], "upstream/main")
        self.assertEqual(doc["source"]["revision"], expected)



class MigrateTest(unittest.TestCase):
    def test_committed_v1_witness_migrates_to_v2(self):
        if testlib.llvm_root() is None:
            self.skipTest("X86_MIR_LLVM_SRC is not set")
        source = testlib.witness_path()
        if source is None:
            self.skipTest("committed witness is not available")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "witness-v2.json")
            proc = run_cli("migrate", "--input", source, "--out", out)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(out, encoding="utf-8") as handle:
                doc = json.load(handle)
        self.assertEqual(doc["version"], 2)
        self.assertEqual(doc["migrated_from_version"], 1)
        self.assertEqual(doc["atom_count"], len(doc["atoms"]))
        self.assertTrue(doc["atoms"])
        self.assertTrue(all(atom.get("opcode") for atom in doc["atoms"]))
        self.assertEqual(doc["atoms"][0]["opcode"], "G_ADD")


if __name__ == "__main__":
    unittest.main()
