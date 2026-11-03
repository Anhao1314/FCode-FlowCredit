"""Target adapters: how the auditor reaches the thing it audits.

Planned adapter set (Sprint 2+), one module each:

- ``repo``        — a source tree on disk (static analysis, no execution)
- ``desktop_app`` — an installed desktop application: data directories,
                    permission bits, trust chains; behavior probing only
                    with explicit operator authorization
- ``http``        — an agent exposed over HTTP (OpenAI-compatible or MCP)
- ``mcp``         — an agent reachable as an MCP server via the runtime's
                    existing MCP bridge

Adapter interface shape: ``profile() → launch()/attach() → send() →
collect() → teardown()``. Adapters report what they observed; they do
not decide severity — that belongs to rules and the verifier.
"""
