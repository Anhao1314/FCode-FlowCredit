# REA artifact fixtures

Synthetic facts for the two `trigger.type = "rea"` rules. They are data,
not a shipped application: the real adapter is exercised by
`tests/test_rea_integration.py` with a fake REA CLI.

- `facts-prerelease.json` — a Go dependency graph with one pre-release row.
  Expected: `RULE-AGI05-101` candidate; the host oracle requires the row.
- `facts-entitlements.json` — macOS entitlements outside the declared
  feature list. Expected: `RULE-AGI07-101` candidate; the host computes the
  difference, not REA.

The control projection is derived by the adapter (remove pre-release rows;
declare every entitlement), so these files are deliberately *not* clean.

- `sidecar-unauth.json` — a sidecar listener with no auth marker.
  Expected: `RULE-AGI02-102` candidate.
- `sidecar-secret.json` — a key-shaped literal in sidecar source.
  Expected: `RULE-AGI03-102` candidate.
- `sidecar-npm.json` — a sidecar source reference to npm install/exec.
  Expected: `RULE-AGI05-102` candidate.

- `osv-snapshot.json` — synthetic OSV record for `example/rc` (< v1.0.0).
- `facts-vulnerable.json` — a host vulnerability row produced from that
  snapshot. Expected: `RULE-AGI05-103` candidate.

- `facts-runtime-install.json` — a main-binary runtime `npm install -g`
  command row. Expected: `RULE-AGI05-104` candidate.
