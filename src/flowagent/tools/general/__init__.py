"""Governed general capabilities: the reference implementation.

The finance domain proves the runtime on numbers it can recompute. This
package proves it on the two capabilities every general agent needs — writing
files and fetching the web — *under the same governance*: the gate decides,
the budget charges, every result carries a receipt the runtime can re-derive
(file-state hashes), and what the runtime cannot verify (web content truth)
is labelled not-verified instead of being laundered into "checked".

Nothing here belongs to a demo domain. The finance module and this module are
peers: both register closed toolsets, both stay behind the same gate.
"""
