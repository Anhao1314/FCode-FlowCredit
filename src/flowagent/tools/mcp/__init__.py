"""The MCP bridge: the official import channel for external tools.

FCode does not grow every capability itself; it governs whatever is attached.
An MCP server's tools enter the *same closed registry* as native ones — same
gate, same budget, same receipts, same unknown discipline — under names the
host can audit (``mcp__{server}__{tool}``). Two authorities stay with the
host and never with the server or the model:

* **which servers exist** — ``mcp.json`` at the repository root, reviewed and
  edited by the host before it takes effect;
* **which imported tools are read-only** — an MCP server's own description is
  a claim, not a classification. Everything imports as consequential
  (write-effect, approval-gated) unless the host marks the tool readonly in
  ``mcp.json``.
"""
