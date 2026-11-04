"""AArch64 target vocabulary for the legalizer-corpus reader.

AArch64LegalizerInfo.cpp enables extended LLT kinds in its TargetMachine, so
``LLT::integer(n)`` is ``Kind::INTEGER`` (``iN``) and is *not* equal to
``LLT::scalar(n)`` (``sN``).  Both spellings appear in the source and the type
axes below keep them separate.
"""

from .base import TargetProfile

FEATURE_KEYS = (
    "is64Bit",
    "hasNEON",
    "hasFPARMv8",
    "hasFullFP16",
    "hasCSSC",
    "hasRCPC3",
    "hasSVE",
    "hasBF16",
    "hasLSE",
    "hasMOPS",
    "outlineAtomics",
    "codeModel",
)

_BASE = {
    "is64Bit": True,
    "hasNEON": True,
    "hasFPARMv8": True,
    "hasFullFP16": False,
    "hasCSSC": False,
    "hasRCPC3": False,
    "hasSVE": False,
    "hasBF16": False,
    "hasLSE": False,
    "hasMOPS": False,
    "outlineAtomics": False,
    "codeModel": "Small",
}

CONFIGS = {
    "A0": dict(_BASE),
    "A1": dict(_BASE, hasFullFP16=True),
    "A2": dict(_BASE, hasFullFP16=True, hasCSSC=True, hasRCPC3=True,
               hasSVE=True, hasBF16=True, hasLSE=True, hasMOPS=True),
    "A3": dict(_BASE, hasFullFP16=True, hasCSSC=True, hasRCPC3=True,
               hasSVE=True, hasBF16=True, hasLSE=True, hasMOPS=True,
               outlineAtomics=True),
}

CONFIG_INFO = {
    "A0": {"triple": "aarch64", "fset": "+neon,+fp-armv8"},
    "A1": {"triple": "aarch64", "fset": "+neon,+fp-armv8,+fullfp16"},
    "A2": {"triple": "aarch64",
           "fset": "+neon,+fp-armv8,+fullfp16,+sve,+cssc,+rcpc3,+bf16,"
                   "+lse,+mops"},
    "A3": {"triple": "aarch64",
           "fset": "+neon,+fp-armv8,+fullfp16,+sve,+cssc,+rcpc3,+bf16,"
                   "+lse,+mops,+outline-atomics"},
}

_BITS = (8, 16, 32, 64, 128)
_FLOAT = (("f", 16, "ieee"), ("f", 32, "ieee"), ("f", 64, "ieee"),
          ("f", 128, "ieee"), ("f", 16, "bfloat"))
_FIXED_VECTOR_SHAPES = ((2, 8), (4, 8), (8, 8), (16, 8), (2, 16), (4, 16),
                       (8, 16), (2, 32), (4, 32), (2, 64))
_SCALABLE_VECTOR_SHAPES = ((16, 8), (8, 16), (4, 32), (2, 64))
_FLOAT_VECTOR_SHAPES = ((4, ("f", 16, "ieee")), (8, ("f", 16, "ieee")),
                       (4, ("f", 16, "bfloat")), (8, ("f", 16, "bfloat")),
                       (2, ("f", 32, "ieee")), (4, ("f", 32, "ieee")),
                       (2, ("f", 64, "ieee")))


def _vector_panel():
    panel = []
    for lanes, bits in _FIXED_VECTOR_SHAPES:
        panel.append(("v", lanes, ("s", bits)))
        panel.append(("v", lanes, ("i", bits)))
    panel.append(("v", 2, ("p", 0)))
    for lanes, elem in _FLOAT_VECTOR_SHAPES:
        panel.append(("v", lanes, elem))
    for lanes, bits in _SCALABLE_VECTOR_SHAPES:
        panel.append(("nxv", lanes, ("s", bits)))
        panel.append(("nxv", lanes, ("i", bits)))
    return tuple(panel)


# The reviewed boundary/witness panel.  It covers every LLT kind the AArch64
# legalizer mentions, including the iN/sN split and scalable vectors.
BOUNDARY_PANEL = (
    *(("s", bits) for bits in _BITS),
    *(("i", bits) for bits in _BITS),
    ("p", 0),
    *_FLOAT,
    *_vector_panel(),
)

# The deep panel adds odd sizes/lane counts that exercise widening and
# element-count clamping without exploding the full corpus.
DEEP_PANEL = (
    *(("s", bits) for bits in (1, 2, 3, 7, 9, 12, 15, 17, 24, 31, 33, 40,
                               48, 63, 65, 96, 100, 127, 129, 192, 200, 256,
                               384, 512, 1024)),
    *(("i", bits) for bits in (1, 3, 7, 24, 33, 65, 100, 129, 200)),
    *_FLOAT,
    ("v", 2, ("s", 8)), ("v", 4, ("s", 8)), ("v", 8, ("s", 8)),
    ("v", 16, ("s", 8)), ("v", 2, ("s", 16)), ("v", 4, ("s", 16)),
    ("v", 8, ("s", 16)), ("v", 2, ("s", 32)), ("v", 4, ("s", 32)),
    ("v", 2, ("s", 64)), ("v", 2, ("p", 0)),
    ("v", 3, ("s", 8)), ("v", 3, ("s", 16)), ("v", 3, ("s", 32)),
    ("v", 5, ("s", 32)), ("v", 6, ("s", 16)), ("v", 7, ("s", 32)),
    ("v", 3, ("f", 16, "ieee")), ("v", 3, ("f", 32, "ieee")),
    ("v", 5, ("f", 32, "ieee")), ("v", 3, ("f", 16, "bfloat")),
    ("nxv", 16, ("s", 8)), ("nxv", 8, ("s", 16)),
    ("nxv", 4, ("s", 32)), ("nxv", 2, ("s", 64)),
    ("nxv", 3, ("s", 32)), ("nxv", 2, ("f", 16, "ieee")),
    ("nxv", 4, ("f", 32, "ieee")), ("nxv", 2, ("f", 64, "ieee")),
)


def _aliases(features):
    return {
        "Is64Bit": features["is64Bit"],
        "HasNEON": features["hasNEON"],
        "HasFPARMv8": features["hasFPARMv8"],
        "HasFP16": features["hasFullFP16"],
        "HasCSSC": features["hasCSSC"],
        "HasRCPC3": features["hasRCPC3"],
        "HasSVE": features["hasSVE"],
        "HasBF16": features["hasBF16"],
        "HasLSE": features["hasLSE"],
        "HasMOPS": features["hasMOPS"],
    }


# AArch64 keeps slot-1 address-space probes: the source uses p0 only, and the
# witness budget already treats p0 as the single pointer class.
SKIP_WITNESS_SLOT1_AS = frozenset()


# Shapes the AArch64 pre-legalizer combiner rewrites before the legalizer runs.
# A raw legality query for them is not IR-reachable: llc on the pinned revision
# compiles the equivalent IR (v3i16 abs, v3i1/v5i1 integer arithmetic) without
# asserting, while the legalizer queried directly either ping-pongs between
# minScalarOrEltIf(s16) and minScalarOrEltIf(s8) or trips
# mutationIsSane(mutation invalid for match).  Excluding them keeps the witness
# focused on legalizer-boundary shapes the pipeline can produce; the rules stay
# visible as pipeline_unreachable coverage issues.
_UNREACHABLE_PROBE_TYPES = (
    ("G_ABS", "v3s16"),
    ("G_ADD", "v3s1"), ("G_ADD", "v5s1"),
    ("G_AND", "v3s1"), ("G_AND", "v5s1"),
    ("G_OR", "v3s1"), ("G_OR", "v5s1"),
    ("G_SUB", "v3s1"), ("G_SUB", "v5s1"),
    ("G_XOR", "v3s1"), ("G_XOR", "v5s1"),
    ("G_MUL", "v3s1"), ("G_MUL", "v5s1"),
)
RULE_PROBE_EXCLUSIONS = frozenset(
    (opcode, (types,)) for opcode, types in _UNREACHABLE_PROBE_TYPES)


PROFILE = TargetProfile(
    name="aarch64",
    source_path="llvm/lib/Target/AArch64/GISel/AArch64LegalizerInfo.cpp",
    rule_set_prefix="AArch64_",
    default_config="A2",
    feature_keys=FEATURE_KEYS,
    configs=CONFIGS,
    config_info=CONFIG_INFO,
    boundary_panel=BOUNDARY_PANEL,
    deep_panel=DEEP_PANEL,
    skip_witness_slot1_as=SKIP_WITNESS_SLOT1_AS,
    llt_extended=True,
    probe_opcode_arity=True,
    rule_probe_exclusions=RULE_PROBE_EXCLUSIONS,
    feature_aliases=_aliases,
)
