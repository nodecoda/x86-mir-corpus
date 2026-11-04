"""The surface/triage docs must match the table and the pinned sources."""

import os
import subprocess
import unittest

import testlib
from x86_mir_corpus import dsl, pin

DOCS = os.path.join(testlib.TOOL_DIR, "docs")


def read(name):
    with open(os.path.join(DOCS, name), encoding="utf-8") as handle:
        return handle.read()


class SurfaceDocTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.table = dsl.load()
        cls.surface = read("DSL-SURFACE.md")
        cls.triage = read("TRIAGE.md")

    def test_every_table_section_is_documented(self):
        for section in ("methods", "predicates", "mutations", "llt_constructors",
                        "llt_queries", "element_count", "query_api",
                        "legalizer_info_api", "helper_entrypoints", "opcodes",
                        "enums", "namespaces", "ignore", "target_functions",
                        "target_queries"):
            self.assertIn(f"`{section}`", self.surface, section)

    def test_every_construction_detail_code_is_documented(self):
        for code in ("mutation_type_idx", "mutation_source_slot", "shape_arity",
                     "imm_idx", "imm_coverage"):
            self.assertIn(f"`{code}`", self.surface, code)

    def test_triage_covers_every_legalize_action(self):
        for entry in self.table["actions"]:
            self.assertIn(f"`{entry['name']}`", self.triage, entry["name"])

    def test_helper_entrypoints_are_named_in_triage(self):
        for entry in self.table["helper_entrypoints"]:
            self.assertIn(entry["name"], self.triage, entry["name"])

    def test_llvm_line_references_resolve(self):
        root = testlib.llvm_root()
        if root is None:
            self.skipTest("set X86_MIR_LLVM_SRC to check pin line references")
        revision = pin.revision()
        anchored = {
            "mutationIsSane": "LegalizerInfo.cpp:116",
            "LegalizeRuleSet::apply": "LegalizerInfo.cpp:191",
            "verifyTypeIdxsCoverage": "LegalizerInfo.cpp:211",
            "verifyImmIdxsCoverage": "LegalizerInfo.cpp:242",
            "getActionDefinitionsBuilder": "LegalizerInfo.cpp:308",
        }
        for anchor, reference in anchored.items():
            self.assertIn(reference, self.surface, anchor)
            line = int(reference.rsplit(":", 1)[1])
            proc = subprocess.run(
                ["git", "-C", root, "show",
                 f"{revision}:llvm/lib/CodeGen/GlobalISel/LegalizerInfo.cpp"],
                capture_output=True, text=True)
            if proc.returncode != 0:
                self.skipTest("pinned revision is not in the checkout")
            body = proc.stdout.splitlines()
            self.assertIn(anchor, body[line - 1], reference)


if __name__ == "__main__":
    unittest.main()
