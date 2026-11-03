"""flowagent — a host-governed, durable, auditable agent runtime.

The package is organised by responsibility, and the dependency direction is
one-way:

    core  ->  tools / providers  ->  runtime  ->  demo

``core`` holds hashing, contracts and storage and imports nothing else from the
package. ``runtime`` may import ``core``, ``tools`` and ``providers``; nothing
imports ``runtime`` except the demo and the tests. That ordering is what keeps
the security properties testable in isolation.
"""

from __future__ import annotations

__version__ = "0.2.0"
__all__ = ["__version__"]
