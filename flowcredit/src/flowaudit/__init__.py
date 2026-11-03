"""flowaudit — the security audit product layered on the flowagent runtime.

flowagent is the kernel: event chain, gating, budget, recovery, replay.
flowaudit is what that kernel is for now: an agent that audits *other*
agent applications for defects and security issues, where every finding
carries a re-playable proof of concept and no finding enters a report
until an independent verifier re-runs that proof.

The dependency arrow points exactly one way:

    flowaudit → flowagent   (allowed, this is the design)
    flowagent → flowaudit   (forbidden, enforced by tests/test_module_boundary.py)

The kernel must stay domain-agnostic as a property, not a hope. If
flowagent ever learns about audits, the claim dies with the import.

Status (runtime-v1 → Sprint 1): skeleton. The taxonomy lives in
``flowaudit.vulns``, the methodology in ``docs/audit-methodology.md``,
the rule contract in ``docs/rule-authoring.md``. Sprint 2 fills the
rule engine and the PoC replay loop.
"""
