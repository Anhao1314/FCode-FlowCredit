# LangGraph Three-Agent Pilot

A deliberately isolated pilot, not a production path. It tests whether
LangGraph gives FCode a better multi-agent substrate than the hand-rolled
`GovBotService`:

```text
START -> Auditor -> Approval --(approved)--> Planner -> Verifier -> END
                       |
                       +--(rejected)--> END
```

- **Auditor** runs the real offline audit CLI into a lab-local bundle.
- **Approval** pauses with `interrupt()` and resumes via `Command(resume=...)`.
- **Planner** turns the top finding into a deterministic repair brief.
- **Verifier** recomputes the handoff hash chain and checks plan evidence.
- Handoffs are written to `manifest.jsonl`, independent of LangGraph's
  checkpoint. The manifest remains the truth; the checkpoint only resumes
  control flow.
- Checkpointing uses `MemorySaver` (same-process resume). A SQLite checkpointer
  is a later step; it must not become the only evidence store.

## Setup

The main project is stdlib-only and currently targets Python 3.9 in this
machine's default shell. Use a separate Python 3.12 venv for the pilot:

```bash
cd flowcredit
uv venv --python 3.12 .venv-langgraph
uv pip install --python .venv-langgraph/bin/python -r labs/langgraph_swarm/requirements.txt
```

## Run

```bash
.venv-langgraph/bin/python -m labs.langgraph_swarm.cli \
  --target fixtures/demo-target --auto-approve
```

Interactive approval:

```bash
.venv-langgraph/bin/python -m labs.langgraph_swarm.cli \
  --target fixtures/demo-target --interactive
```

Without `--auto-approve` or `--interactive`, the process prints the pending
approval and exits 2; `MemorySaver` is process-local, so use one of the two
resume flags for an end-to-end run.

Artifacts:

```text
labs/langgraph_swarm/.runs/<run_id>/
  audit/audit-bundle.json
  manifest.jsonl
  result.json
```

## Tests

```bash
.venv-langgraph/bin/python -m unittest tests.test_langgraph_swarm -v
```

The stdlib gate skips these when LangGraph is absent.

## What this proves

- Graph-level orchestration and an approval interrupt.
- Resume after interrupt without re-running completed nodes.
- Handoff hashes chained outside the framework checkpoint.
- Real audit output can be a node result without giving the agent tool
  authority.

## Explicit non-goals

- Not wired into the viewer or `GovBotService`.
- No LLM-to-LLM conversation; the roles are deterministic.
- No cross-process checkpoint yet.
- Not a replacement for FCode's gate/budget/HMAC chain.