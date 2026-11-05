# EvoX target descriptor

`target.md` is the authorization/scope/feature declaration for a local
static audit of `/Applications/EvoX.app` (1.1.0). It is a descriptor, not
the application: no EvoX bytes are committed.

```bash
python audit.py audit /Applications/EvoX.app \
  --adapter rea --target-md fixtures/evox/target.md \
  --out work/audit/evox-2026-11-03
```

The declared features are the operator's claim, not a vendor statement.
`com.apple.security.cs.disable-library-validation` is deliberately left
undeclared: it is a Hardened Runtime weakening, not a user-facing feature,
and must be justified separately. The current ruleset therefore reports
one AGI-07 candidate for it; the host oracle decides the finding.

The generated bundle and `report.md` live under `work/audit/` (ignored by
git), together with the raw REA Evidence and the facts/control snapshots.
