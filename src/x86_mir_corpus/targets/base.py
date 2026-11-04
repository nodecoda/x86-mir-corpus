"""Target profile shared by every supporting backend.

Everything the parser/domain layer needs to know about a target lives here:
the feature vocabulary, reviewed configs, boundary panels, the rule-set name
prefix, and the LLT mode (extended kinds on/off).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TargetProfile:
    name: str
    source_path: str
    rule_set_prefix: str
    default_config: str
    feature_keys: tuple
    configs: Mapping[str, dict]
    config_info: Mapping[str, dict]
    boundary_panel: tuple
    deep_panel: tuple
    skip_witness_slot1_as: frozenset
    llt_extended: bool = False
    # Probe rule shapes with the opcode's declared type arity instead of the
    # largest slot index a rule mentions.  AArch64 rules often constrain a
    # pair (src, amount) through ``legalFor({{...}, ...})`` without naming
    # slot 1 in any argument, so slot-index inference undercounts the query
    # and leaves those rules undecided.
    probe_opcode_arity: bool = False
    # Rule-witness shapes (opcode, type-name tuple) that are excluded from
    # execution because the pre-legalizer pipeline normalises them before the
    # legalizer can ever see them.  Each entry needs reviewed evidence; the
    # rule is reported as a pipeline_unreachable coverage issue, never silently
    # counted as verified.
    rule_probe_exclusions: frozenset = frozenset()
    feature_aliases: Callable[[dict], dict] | None = field(default=None)

    def features(self, config):
        return self.configs[config]

    def config_descriptor(self, config):
        return {
            "target": self.name,
            "id": config,
            "triple": self.config_info[config]["triple"],
            "fset": self.config_info[config]["fset"],
            "features": self.configs[config],
        }

    def pointer_bits(self, config):
        return 64 if self.configs[config]["is64Bit"] else 32

    def feature_env(self, features):
        """Eval environment: features plus the target's local aliases."""
        env = dict(features)
        env["__features__"] = features
        env["__code_model__"] = features.get("codeModel")
        for value in ("Tiny", "Small", "Kernel", "Large", "LargePIC"):
            env[f"CodeModel::{value}"] = value
        if self.feature_aliases is not None:
            env.update(self.feature_aliases(features))
        return env
