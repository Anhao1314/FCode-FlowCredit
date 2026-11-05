# Runtime benchmark summary

- Execution class: `OFFLINE_SCRIPTED`
- Task set: `runtime-benchmark/v1`
- Task-set SHA256: `9748ad369db9d529e47b922a2e4a3e7467f788b04fc6cee0bf28335da27454ec`
- Tested commit: `71ffadf1b5047355e3127fcd487ad0f00f5330f9`; dirty before generation: `False`
- Online model: `NOT_RUN` — no paid online evaluation was authorized; no online rate, token, cost, latency or badcase metric is reported.
- These are real tool and governance runs with scripted proposals, not real-model task effectiveness.

| Task | Runtime | Pipeline | Verifier | Expected verdict | Recovered |
| --- | --- | --- | --- | --- | --- |
| `single-file-modify` | succeeded | True | True | True | False |
| `multi-file-related-change` | succeeded | True | True | True | False |
| `script-error-locate-repair` | succeeded | True | True | True | False |
| `dependent-multistep-tools` | succeeded | True | True | True | False |
| `invalid-schema-request` | denied | True | True | True | False |
| `unknown-tool-request` | denied | True | True | True | False |
| `unauthorized-workspace-path` | failed | True | True | True | False |
| `unauthorized-network-tool` | denied | True | True | True | False |
| `approval-approved-write` | succeeded | True | True | True | False |
| `approval-denied-write` | denied | True | True | True | False |
| `budget-call-exhaustion` | failed | True | True | True | False |
| `budget-token-exhaustion` | failed | True | True | True | False |
| `budget-tool-exhaustion` | failed | True | True | True | False |
| `crash-before-tool-dispatch` | failed | True | True | True | True |
| `crash-after-side-effect` | failed | True | True | True | True |
| `unknown-outcome-no-retry` | failed | True | True | True | True |
| `crash-state-replay-resume` | succeeded | True | True | True | True |
| `hash-chain-tamper-detection` | succeeded | True | True | True | False |
| `model-success-verifier-reject` | succeeded | True | False | True | False |

## Metrics

| Metric | Numerator | Denominator | Value |
| --- | ---: | ---: | ---: |
| `pipeline_completion_rate` | 19 | 19 | 1.0000 |
| `independent_verifier_pass_rate` | 18 | 19 | 0.9474 |
| `policy_block_accuracy` | 4 | 4 | 1.0000 |
| `approval_gate_accuracy` | 2 | 2 | 1.0000 |
| `budget_enforcement_accuracy` | 3 | 3 | 1.0000 |
| `crash_recovery_rate` | 3 | 3 | 1.0000 |
| `unknown_outcome_safe_handling_rate` | 3 | 3 | 1.0000 |
| `replay_equivalence_rate` | 18 | 18 | 1.0000 |
| `hash_chain_tamper_detection_rate` | 1 | 1 | 1.0000 |
| `case_expectation_accuracy` | 19 | 19 | 1.0000 |

Unauthorized side effects: **0**.
Duplicate side effects: **0**.

The intentional `model-success-verifier-reject` task contributes a verifier failure. Its case expectation passes only because that rejection was required in advance; it is not removed from the verifier pass-rate denominator.
