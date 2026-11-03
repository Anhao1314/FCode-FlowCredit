"""flowagent — a host-governed, durable, auditable agent runtime.

The package is organised by responsibility, and the dependency direction is
one-way:

    core  ->  tools / providers / memory  ->  runtime  ->  surfaces

``core`` holds the cross-layer vocabulary (contracts, observation, packets,
verification, facts), hashing and storage, and imports nothing else from the
package. ``memory`` and ``tools`` are siblings: both depend on ``core`` and
neither imports the other. ``runtime`` may import all three. Surfaces (demo,
chat, benchmark, sft, flowaudit) may import the runtime; the runtime never
imports a surface. That ordering is what keeps the security properties
testable in isolation.
"""

from __future__ import annotations

__version__ = "0.2.0"
__all__ = ["__version__"]
