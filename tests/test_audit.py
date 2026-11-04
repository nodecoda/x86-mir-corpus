"""Region-aware DSL audit: constructor/lambda gate, lowering is separate."""

import os
import unittest

import testlib  # noqa: F401  (sets sys.path)
from x86_mir_corpus import audit, pin

KNOWN = """\
AArch64LegalizerInfo::AArch64LegalizerInfo(const AArch64Subtarget &ST) {
  const LLT s32 = LLT::scalar(32);
  getActionDefinitionsBuilder(G_ADD)
      .legalFor({s32})
      .widenScalarIf([](const LegalityQuery &Q) {
        return Q.Types[0].getScalarType().isFloat16();
      }, changeTo(0, s32));
}
"""

UNKNOWN_CONSTRUCTOR = """\
AArch64LegalizerInfo::AArch64LegalizerInfo(const AArch64Subtarget &ST) {
  const LLT s32 = LLT::scalar(32);
  getActionDefinitionsBuilder(G_ADD).widenScalarToNextPow3(0);
}
"""

UNKNOWN_LAMBDA = """\
AArch64LegalizerInfo::AArch64LegalizerInfo(const AArch64Subtarget &ST) {
  getActionDefinitionsBuilder(G_ADD)
      .widenScalarIf([](const LegalityQuery &Q) {
        return Q.Types[0].isTotallyBogus();
      }, changeTo(0, LLT::scalar(32)));
}
"""

LOWERING_ONLY = """\
AArch64LegalizerInfo::AArch64LegalizerInfo(const AArch64Subtarget &ST) {
  getActionDefinitionsBuilder(G_ADD).legalFor({LLT::scalar(32)});
}

bool AArch64LegalizerInfo::legalizeCustom(LegalizerHelper &Helper,
                                          MachineInstr &MI) const {
  return Helper.buildInstr(MI.getOpcode()).getReg() != 0;
}
"""


def snapshot(target):
    doc = pin.load(target=target)
    if not doc or not doc.get("snapshot"):
        return None
    path = os.path.join(pin.package_dir(), doc["snapshot"])
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class AuditTextTest(unittest.TestCase):
    def test_known_constructor_and_lambda_are_clean(self):
        report = audit.audit_text(KNOWN, "aarch64")
        self.assertEqual(report["scopes"]["constructor"]["unknown"], 0)
        self.assertEqual(report["scopes"]["lambda"]["unknown"], 0)
        self.assertTrue(report["scopes"]["constructor"]["exact"] > 0)
        self.assertEqual(audit.gate(report, strict=False), [])

    def test_unknown_chain_method_fails_the_constructor_gate(self):
        report = audit.audit_text(UNKNOWN_CONSTRUCTOR, "aarch64")
        self.assertEqual(report["scopes"]["constructor"]["unknown"], 1)
        unknown = [u for u in report["unknown"] if u["scope"] == "constructor"]
        self.assertEqual(unknown[0]["name"], "widenScalarToNextPow3")
        self.assertEqual(unknown[0]["line"], 3)
        self.assertTrue(audit.gate(report, strict=False))

    def test_unknown_llt_query_fails_the_lambda_gate(self):
        report = audit.audit_text(UNKNOWN_LAMBDA, "aarch64")
        self.assertEqual(report["scopes"]["lambda"]["unknown"], 1)
        self.assertEqual(
            [u["name"] for u in report["unknown"] if u["scope"] == "lambda"],
            ["isTotallyBogus"])
        self.assertTrue(audit.gate(report, strict=False))

    def test_lowering_unknowns_are_reported_but_not_gated(self):
        report = audit.audit_text(LOWERING_ONLY, "aarch64")
        self.assertGreater(report["scopes"]["lowering"]["unknown"], 0)
        self.assertEqual(report["scopes"]["constructor"]["unknown"], 0)
        self.assertEqual(audit.gate(report, strict=False), [])

    def test_surface_hash_is_stable_and_scope_limited(self):
        first = audit.audit_text(KNOWN, "aarch64")
        second = audit.audit_text(KNOWN, "aarch64")
        self.assertEqual(first["surface_sha256"], second["surface_sha256"])
        # A lowering-only change must not move the gated surface hash.
        other = audit.audit_text(
            KNOWN + "\nbool AArch64LegalizerInfo::legalizeCustom("
            "LegalizerHelper &H, MachineInstr &MI) const { "
            "return H.buildInstr(MI.getOpcode()).getReg() != 0; }\n",
            "aarch64")
        self.assertEqual(first["surface_sha256"], other["surface_sha256"])


class SnapshotAuditTest(unittest.TestCase):
    def test_bundled_snapshots_have_no_gated_unknowns(self):
        for target in ("x86", "aarch64"):
            text = snapshot(target)
            if text is None:
                self.skipTest(f"{target} snapshot not bundled")
            report = audit.audit_text(text, target)
            self.assertEqual(report["scopes"]["constructor"]["unknown"], 0,
                             msg=f"{target} constructor")
            self.assertEqual(report["scopes"]["lambda"]["unknown"], 0,
                             msg=f"{target} lambda")
            self.assertEqual(audit.gate(report, strict=False), [],
                             msg=f"{target} gate")

    def test_manifests_match_the_bundled_snapshots(self):
        for target in ("x86", "aarch64"):
            text = snapshot(target)
            if text is None:
                self.skipTest(f"{target} snapshot not bundled")
            report = audit.audit_text(text, target)
            manifest = audit.load_manifest(target)
            self.assertIsNotNone(manifest, msg=f"{target} manifest missing")
            self.assertEqual(manifest["surface_sha256"],
                             report["surface_sha256"], msg=target)
            self.assertEqual(manifest["scopes"], report["scopes"], msg=target)


if __name__ == "__main__":
    unittest.main()
