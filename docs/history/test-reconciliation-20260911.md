# CTF historical test reconciliation — 2026-09-11

The reconstructed CTF release mixed runtime code with tests for an older Agent
API and scheduler. The initial full run reported 22 failures and two collection
errors, with 151 passing tests. Repairing collection exposed additional stale
assumptions in the scheduler tests.

## Contract updates

| Historical assumption | Current contract and regression coverage |
| --- | --- |
| `decorate_worker_prompt` and a mutable shared-state file | Per-task graph snapshots; tests preserve graph content and verify distinct execute/conclude snapshots. |
| `RuntimeAgentProfile`, `dynamic_workers`, and `agent_ids` | `AgentRuntime`, server-managed workers and explicit Parent/participant assignments; tests check provider identity, project filtering and busy assignments. |
| Adaptive Agent weights and per-intent exponential backoff | Fixed unhealthy/rejected Worker cooldowns; tests verify fallback selection, expiry and successful Reason checkpoints. |
| Bulk `PUT /agents`, old health field names | Individual `POST /agents` and `PUT /agents/{id}`; tests check key masking, saved-key retention, health state and exports. |
| Single multi-Agent project with a removed legacy bootstrap | Distinct Parent/Child slots; tests verify Parent delegation and reject overlapping, missing or disabled assignments. |
| Task timeouts persisted in global settings | Task timeouts remain in dispatcher configuration; migration tests verify persisted lease values and orchestration defaults. |
| Fake conclude command returns a list; completion has no evidence argument | Test doubles now use `DriverResult` and the current completion signature, including optional evidence. |

Obsolete feature assertions were replaced with current contract checks. Their
removed APIs were not reintroduced as placeholder implementations. Existing Flag
candidate, completion, attachment, database and runtime tests remain part of the
full suite.

## Runtime repairs

- Host Python output defaults to UTF-8, preserving non-ASCII working directories.
- Windows termination force-kills the process tree with a bounded command wait,
  and falls back to killing the direct process if it cannot be reaped.
- Local mock workers use the running Python interpreter; container workers retain
  `python3`. Container-style end-to-end test doubles map that name to the host
  interpreter without requiring a Windows application alias.
- Local Codex execution defaults to `workspace-write`, respects configured
  sandbox/network settings, supplies explicit provider settings when present,
  and extracts sessions and final responses from JSONL. Resume parameters were
  checked against the installed CLI help without starting an Agent invocation.
- `dispatch.codex.yaml` uses `server_managed_agents`, so its empty initial Worker
  list passes configuration validation and is populated from the server.

## Validation

Full CTF suite: **215 passed, 4 skipped**, with five dependency deprecation
warnings. Two skips require a project-local Pi installation; two are POSIX-only
fake shell CLI tests. Windows process-tree tests and the POSIX signal escalation
unit test pass. No new skip markers were added to hide failures.

The suite used the available sibling Python environment with `PYTHONPATH` set to
this release's `cairn/src`, and isolated temporary databases/workspaces. No live
model request, service restart, commit or GitHub push was performed.
