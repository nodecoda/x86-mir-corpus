"""X86 target vocabulary for the C++ legalizer-corpus reader.

Only this module knows that the input is X86: the feature environment,
the LLT spellings used by the source, and the reviewed boundary panels.
"""

from .pin import default_source_rev as _default_source_rev

# The Subtarget predicates the X86 legalizer constructor reads.  A new one in
# the C++ source is a hard error until it is added here (see parse.py).
FEATURE_KEYS = (
    "is64Bit",
    "canUseCMOV",
    "hasSSE1",
    "hasSSE2",
    "hasSSE41",
    "hasAVX",
    "hasAVX2",
    "hasAVX512",
    "hasVLX",
    "hasDQI",
    "hasBWI",
    "useSoftFloat",
    "hasX87",
    "hasPOPCNT",
    "hasLZCNT",
    "hasBMI",
    "hasVPOPCNTDQ",
    "isTarget64BitLP64",
)

# Configs mirror the four FullConfigs in X86LegalizerInfoDifferentialTest.cpp.
# C2 is the witness config and is fully featured, so every guard is taken.
CONFIGS = {
    "C0": {
        "is64Bit": False, "canUseCMOV": False, "hasSSE1": False, "hasSSE2": False,
        "hasSSE41": False, "hasAVX": False, "hasAVX2": False, "hasAVX512": False,
        "hasVLX": False, "hasDQI": False, "hasBWI": False, "useSoftFloat": False,
        "hasX87": False, "hasPOPCNT": False, "hasLZCNT": False, "hasBMI": False,
        "hasVPOPCNTDQ": False, "isTarget64BitLP64": False,
    },
    "C1": {
        "is64Bit": True, "canUseCMOV": True, "hasSSE1": True, "hasSSE2": True,
        "hasSSE41": False, "hasAVX": False, "hasAVX2": False, "hasAVX512": False,
        "hasVLX": False, "hasDQI": False, "hasBWI": False, "useSoftFloat": False,
        "hasX87": True, "hasPOPCNT": False, "hasLZCNT": False, "hasBMI": False,
        "hasVPOPCNTDQ": False, "isTarget64BitLP64": True,
    },
    "C2": {
        "is64Bit": True, "canUseCMOV": True, "hasSSE1": True, "hasSSE2": True,
        "hasSSE41": True, "hasAVX": True, "hasAVX2": True, "hasAVX512": True,
        "hasVLX": True, "hasDQI": True, "hasBWI": True, "useSoftFloat": False,
        "hasX87": True, "hasPOPCNT": True, "hasLZCNT": True, "hasBMI": True,
        "hasVPOPCNTDQ": True, "isTarget64BitLP64": True,
    },
    "C3": {
        "is64Bit": False, "canUseCMOV": True, "hasSSE1": False, "hasSSE2": False,
        "hasSSE41": False, "hasAVX": False, "hasAVX2": False, "hasAVX512": False,
        "hasVLX": False, "hasDQI": False, "hasBWI": False, "useSoftFloat": False,
        "hasX87": True, "hasPOPCNT": True, "hasLZCNT": True, "hasBMI": True,
        "hasVPOPCNTDQ": False, "isTarget64BitLP64": False,
    },
}

# The reviewed 12-atom witness/full panel.  This is a review constant, not a
# property derived from the C++ source; changes to it are visible in review.
BOUNDARY_PANEL = (
    ("s", 3), ("s", 7), ("s", 24), ("s", 33), ("s", 65), ("s", 100),
    ("s", 129), ("s", 200), ("s", 256),
    ("v", 3, ("s", 32)), ("v", 16, ("s", 16)), ("v", 64, ("s", 8)),
)

# The reviewed 60-atom deep panel (x86-legalizer-deep-panel.json).
DEEP_PANEL = (
    ("s", 100), ("s", 1024), ("s", 12), ("s", 127), ("s", 129), ("s", 15),
    ("s", 160), ("s", 17), ("s", 192), ("s", 2), ("s", 20), ("s", 200),
    ("s", 224), ("s", 23), ("s", 24), ("s", 255), ("s", 256), ("s", 3),
    ("s", 31), ("s", 33), ("s", 384), ("s", 4), ("s", 40), ("s", 48),
    ("s", 5), ("s", 512), ("s", 6), ("s", 63), ("s", 65), ("s", 7),
    ("s", 72), ("s", 9), ("s", 96),
    ("v", 12, ("s", 16)), ("v", 12, ("s", 32)), ("v", 16, ("s", 16)),
    ("v", 16, ("s", 64)), ("v", 2, ("s", 128)), ("v", 2, ("s", 16)),
    ("v", 2, ("s", 8)), ("v", 32, ("s", 32)), ("v", 32, ("s", 64)),
    ("v", 3, ("s", 128)), ("v", 3, ("s", 16)), ("v", 3, ("s", 32)),
    ("v", 3, ("s", 64)), ("v", 3, ("s", 8)), ("v", 4, ("s", 128)),
    ("v", 4, ("s", 16)), ("v", 5, ("s", 32)), ("v", 5, ("s", 64)),
    ("v", 64, ("s", 16)), ("v", 64, ("s", 8)), ("v", 6, ("s", 16)),
    ("v", 6, ("s", 32)), ("v", 6, ("s", 64)), ("v", 7, ("s", 16)),
    ("v", 7, ("s", 32)), ("v", 7, ("s", 64)), ("v", 8, ("s", 8)),
)

# Rule sets whose slot-1 address-space points are skipped by the reviewed
# witness budget (X86LegalizerInfoDifferentialTest.cpp:skipsWitnessSlot1AS).
SKIP_WITNESS_SLOT1_AS = {
    "X86_MERGE_VALUES", "X86_UNMERGE_VALUES", "X86_ANYEXT", "X86_SEXT",
    "X86_FCMP",
}

DEFAULT_CONFIG = "C2"
DEFAULT_SOURCE_PATH = "llvm/lib/Target/X86/GISel/X86LegalizerInfo.cpp"
# Pinned LLVM commit behind the bundled snapshot.  ``pin show`` prints it and
# ``pin update`` moves it; the fallback only applies to an installed package
# that lost its assets.
DEFAULT_SOURCE_REV = _default_source_rev()

# The C++ constructor spells the Subtarget predicates with local aliases.  A
# lambda body is scanned as tokens, so it needs the same aliases the parser's
# environment had.
def feature_env(features):
    env = dict(features)
    env["__features__"] = features
    env["Is64Bit"] = features["is64Bit"]
    env["HasCMOV"] = features["canUseCMOV"]
    env["HasSSE1"] = features["hasSSE1"]
    env["HasSSE2"] = features["hasSSE2"]
    env["HasSSE41"] = features["hasSSE41"]
    env["HasAVX"] = features["hasAVX"]
    env["HasAVX2"] = features["hasAVX2"]
    env["HasAVX512"] = features["hasAVX512"]
    env["HasVLX"] = features["hasVLX"]
    env["HasDQI"] = features["hasAVX512"] and features["hasDQI"]
    env["HasBWI"] = features["hasAVX512"] and features["hasBWI"]
    env["UseX87"] = (not features["useSoftFloat"]) and features["hasX87"]
    env["HasPOPCNT"] = features["hasPOPCNT"]
    env["HasLZCNT"] = features["hasLZCNT"]
    env["HasBMI"] = features["hasBMI"]
    env["HasVPOPCNTDQ"] = features["hasVPOPCNTDQ"]
    return env


# The four full-corpus configurations, matching FullConfigs in
# X86LegalizerInfoDifferentialTest.cpp.  The C++ filler validates this map
# against the constructed subtarget; a mismatch is a hard error.
CONFIG_INFO = {
    "C0": {"triple": "i386", "fset": "-x87"},
    "C1": {"triple": "x86_64", "fset": ""},
    "C2": {"triple": "x86_64",
           "fset": "+sse,+sse2,+sse4.1,+avx,+avx2,+avx512f,+avx512dq,"
                   "+avx512bw,+avx512vl,+popcnt,+avx512vpopcntdq,+lzcnt,"
                   "+bmi,+cmov"},
    "C3": {"triple": "i386", "fset": "+x87,+popcnt,+lzcnt,+bmi,+cmov"},
}


def config_descriptor(config):
    return {
        "id": config,
        "triple": CONFIG_INFO[config]["triple"],
        "fset": CONFIG_INFO[config]["fset"],
        "features": CONFIGS[config],
    }
