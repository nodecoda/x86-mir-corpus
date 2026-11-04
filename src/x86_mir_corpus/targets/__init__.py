"""Target profiles supported by the legalizer-corpus reader."""

from .aarch64 import PROFILE as AARCH64
from .x86 import PROFILE as X86

PROFILES = {"x86": X86, "aarch64": AARCH64}
DEFAULT = "x86"


def names():
    return sorted(PROFILES)


def get(name=None):
    key = name or DEFAULT
    try:
        return PROFILES[key]
    except KeyError:
        raise ValueError(f"unknown target {key!r}; "
                         f"choose from {', '.join(names())}") from None
