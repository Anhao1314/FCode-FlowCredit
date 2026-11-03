"""The vulnerability taxonomy for agent applications.

Categories are stable identifiers, not prose: rules, findings and reports
all reference them by id, so the taxonomy is the join key between the
rule pack, the detection engine and the report. Renaming a category is a
migration; adding one is not.

A category earns its place by having at least one confirmed real-world
instance in the fixtures (``flowcredit/fixtures/``). The list below is
deliberately small — an audit vocabulary that names everything explains
nothing.
"""

AGI_CATEGORIES: dict[str, str] = {
    "AGI-01": "Indirect prompt injection (untrusted content reaches the agent as instructions)",
    "AGI-02": "Unauthorized tool execution (no gate, self-approval, over-broad remembered approvals)",
    "AGI-03": "Secret leakage (plaintext at rest, into prompts, logs, or telemetry)",
    "AGI-04": "Sandbox escape (path traversal, symlink attacks, command injection)",
    "AGI-05": "Supply chain (unverified self-update, disabled library validation, dependency poisoning)",
    "AGI-06": "Autonomy runaway (self-modification without approval, loops, budget exhaustion, repeated side effects)",
    "AGI-07": "Excessive authority (entitlements or OS grants beyond functional need)",
    "AGI-08": "Governance gaps (tamperable audit logs, missing approval flow, claim-implementation mismatch)",
    "AGI-09": "Honesty defects (claiming completion without verification, substituting unrelated passes)",
}


def category_label(category_id: str) -> str:
    """Return the taxonomy label for a category id, or the id itself."""
    return AGI_CATEGORIES.get(category_id, category_id)
