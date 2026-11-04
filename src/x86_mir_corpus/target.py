"""Compatibility surface for the default (X86) target profile.

New code should take a :class:`x86_mir_corpus.targets.base.TargetProfile` and
read its fields; this module keeps the historical module-level constants so
existing callers and tests keep working.
"""

from . import targets as _targets
from .pin import default_source_rev as _default_source_rev

_X86 = _targets.get("x86")

FEATURE_KEYS = _X86.feature_keys
CONFIGS = _X86.configs
CONFIG_INFO = _X86.config_info
BOUNDARY_PANEL = _X86.boundary_panel
DEEP_PANEL = _X86.deep_panel
SKIP_WITNESS_SLOT1_AS = _X86.skip_witness_slot1_as
DEFAULT_CONFIG = _X86.default_config
DEFAULT_SOURCE_PATH = _X86.source_path
# Pinned LLVM commit behind the bundled snapshot.  ``pin show`` prints it and
# ``pin update`` moves it; the fallback only applies to an installed package
# that lost its assets.
DEFAULT_SOURCE_REV = _default_source_rev()


def get(name=None):
    return _targets.get(name)


def names():
    return _targets.names()


def feature_env(features):
    return _X86.feature_env(features)


def config_descriptor(config):
    return _X86.config_descriptor(config)
