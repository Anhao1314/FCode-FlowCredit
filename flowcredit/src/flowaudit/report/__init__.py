"""Finding schema and report rendering.

A finding is a structured record, not a paragraph:

    id, rule_id, AGI category, severity, confidence,
    target profile, evidence pointers (file:line / request:response),
    PoC reference, replay result, audit-chain entry id

Reports render from findings plus the audit's own event chain. Because
the audit run itself lives in the runtime's hash-chained event log, the
report can answer "who audited the auditor": every step, approval and
replay is re-verifiable from the chain, and the chain verification is
the same ``verify_chain`` the runtime uses on itself.
"""
