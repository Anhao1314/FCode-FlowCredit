# EvoX audit target

## Authorization
- Basis: operator confirms ownership/authorization for a local static audit.
- Scope: `/Applications/EvoX.app`, read-only; no target execution, no network.
- Time window: 2026-11-03.
- Disclosure: private report to the operator; vendor disclosure decision is separate.

## Declared features
- microphone
- calendar
- apple-events
- screen-capture
- local-network
- wifi-name

## Not declared
- `com.apple.security.cs.disable-library-validation`: this is a hardened-runtime
  weakening, not a user-facing feature. It needs a separate justification
  before it can be treated as intentional.

## Known surfaces
- Go/Wails main binary; embedded Vite frontend; bundled Node runtime.
- Sidecar TypeScript sources under `Contents/Resources/*bridge*`.
- Local API/MCP and LAN swarm endpoints are described by REA facts; runtime
  behavior is not observed by this static scope.
