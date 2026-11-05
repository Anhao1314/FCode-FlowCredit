"""Proof-of-concept lifecycle for audit findings (v2, post design review).

The contract this module enforces (Sprint 2):

    detect → candidate → PoC draft → host-validated replay → verdict

Four properties carry the design, each adopted from the 2026-10-31
external design review (docs/design-reviews/2026-10-31-kimi.md):

1. The PoC is a stimulus, never a verdict. The oracle — the invariant
   that decides pass or fail — is defined by the rule, executed and read
   by the host. A PoC that prints "PASS" proves nothing; a hostile or
   buggy PoC cannot promote itself.

2. Every PoC carries a capability manifest (files, network, credentials,
   side effects it needs). The host validates the actual replay against
   the declaration before and during execution. Process isolation is not
   capability isolation: the replay runs with no real credentials, no
   network by default, a temporary filesystem, and a copy of the target.

3. The replay outcome is three-valued, because a FAIL does not prove
   absence: ``finding`` (replay passed under declared scope),
   ``not_reproduced`` (candidate retained, retry or refine),
   ``invalid_poc`` (the harness or oracle itself is broken),
   ``not_vulnerable_under_scope`` (requires negative-control evidence —
   expected to be rare). Only ``finding`` enters a report.

4. A replay record binds evidence: target manifest hash, PoC and oracle
   versions, replayer version, content-addressed references, negative
   control result, and cleanup confirmation. A third party must be able
   to re-run the conclusion from the audit package alone.

Probabilistic defects pre-register their sample count and pass threshold
in the rule; "once succeeded" does not make a finding.
"""
