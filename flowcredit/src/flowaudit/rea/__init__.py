"""REA evidence adapter for artifact targets.

FCode's audit domain owns the verdict. REA is an *evidence source*: it
inspects shipped artifacts (Mach-O/ELF/PE, desktop bundles, JavaScript
applications) and returns a content-addressed ``Evidence`` record. This
package turns that record into bounded, allow-listed facts and keeps the
raw Evidence beside the audit so a third party can re-check it.

The dependency arrow stays inside the product:

    flowaudit.rea → flowagent.core   (allowed)
    flowagent → flowaudit            (forbidden, enforced by tests)

REA output is untrusted input, exactly like target file content: it may
contain strings controlled by the target. Nothing here executes REA
output; facts are data for rules and host-side oracles.
"""

from .client import ReaEvidence, run_evidence, version
from .facts import (
    FACTS_SCHEMA_VERSION,
    build_snapshot_facts,
    extract_desktop_application,
    extract_go_binary,
    extract_javascript_application,
    facts_digest,
)
from .macho import analyze_library_validation, parse_macho
from .osv import load_snapshot, match_go_dependencies
from .sidecars import scan_sidecar, scan_sidecars
from .strings import scan_runtime_installers

__all__ = [
    "FACTS_SCHEMA_VERSION",
    "ReaEvidence",
    "build_snapshot_facts",
    "extract_desktop_application",
    "extract_go_binary",
    "extract_javascript_application",
    "analyze_library_validation",
    "facts_digest",
    "load_snapshot",
    "match_go_dependencies",
    "parse_macho",
    "run_evidence",
    "scan_runtime_installers",
    "scan_sidecar",
    "scan_sidecars",
    "version",
]