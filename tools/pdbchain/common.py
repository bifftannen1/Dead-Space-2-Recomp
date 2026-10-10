"""Helpers shared by pdbchain stages and the legacy tools/naming scripts (one implementation;
pdbchain is canonical). tools/naming imports these instead of keeping its own copies."""
from __future__ import annotations

from .arch.ppc import bl_targets  # noqa: F401  (re-export)
from .gates import wilson_lower as wilson  # noqa: F401
from .paths import ident_for  # noqa: F401


def size_ok(rs, ps):
    """Retail extent (distance to the next registered function, so it includes padding) vs the
    PDB procedure size."""
    return ps > 0 and ps <= rs <= ps + 16 + ps // 16
