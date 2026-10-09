"""Public match-compilation artifacts, diagnostics, and decision contract.

Matrix construction, normalization, allocation, provenance, and compiler
helpers are implementation details available only from their defining
submodules.
"""

from .compiler import CompiledMatchSite
from .diagnostics import match_issue_error
from .model import (
    BoolConstructor,
    Constructor,
    Decision,
    DecisionDecompose,
    DecisionLeaf,
    DecisionSwitch,
    FieldOccurrenceProvenance,
    LiteralKind,
    NominalConstructor,
    Occurrence,
    OccurrenceId,
)
from .stage import (
    CachedModuleSites,
    MatchCompiledProgram,
    cached_module_sites,
    compile_program_matches,
    diagnostics_from_match_issues,
)

__all__ = [
    "BoolConstructor",
    "CachedModuleSites",
    "CompiledMatchSite",
    "Constructor",
    "Decision",
    "DecisionDecompose",
    "DecisionLeaf",
    "DecisionSwitch",
    "FieldOccurrenceProvenance",
    "LiteralKind",
    "MatchCompiledProgram",
    "NominalConstructor",
    "Occurrence",
    "OccurrenceId",
    "cached_module_sites",
    "compile_program_matches",
    "diagnostics_from_match_issues",
    "match_issue_error",
]
