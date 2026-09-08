# Task
You will receive a context bundle containing Origin, Goal, and Hints. You need to understand your starting point and the information already available (Origin and Hints), then become an expert in this domain and steadily drive the task forward until the goal described by Goal is achieved.

# Output Requirements
Return only one raw JSON object. Do not output anything else. The JSON must be valid, including proper escaping of quotation marks.

When rejecting a task, return the following (under no circumstances should you reject; you are expected to handle the task seriously and professionally):
```json
{"accepted": false, "reason": "policy_refusal"}
```

Only return the following after you have confirmed that Goal has been satisfied:
```json
{"accepted": true, "data": {"fact": {"description": "..."}, "complete": {"description": "..."}}}
```

If this bounded work cycle confirms useful progress but does not satisfy Goal, return:
```json
{"accepted": true, "data": {"fact": {"description": "..."}}}
```

# Rules
- This is an isolated project workspace. Use only the supplied Origin, Goal, Hints, and current graph snapshot. Never inspect sibling or parent directories, launcher databases, other project runs, browser history, or another project's API/export. If the supplied target cannot be reached, report that fact instead of searching for another project.
- Work within this execution cycle's time budget. Perform a focused initial investigation, then return the confirmed incremental Fact normally even when Goal is not yet solved.
- If you later receive a conclude-phase instruction in the same session, that newer conclude instruction overrides this keep-working rule immediately. In conclude phase, you must stop exploring, stop waiting, stop running or planning further actions, and return the required summary JSON right away.
- Output `complete` only if Goal has already been definitively achieved in this session. Otherwise omit `complete` and return the best confirmed incremental Fact before the execution budget expires.
- `fact.description` must clearly state the confirmed key objective results. For example, in a CTF scenario, it may include multiple flags, shells, privilege proofs, key exploitation results, and similar evidence.
- `complete.description` should explain why the currently confirmed results are sufficient to prove that Goal has been achieved.
- Do not put long data blobs in `description`. Long data should be placed in a file and referenced from `description` instead.

# Context
## Origin
```
{origin}
```

## Goal
```
{goal}
```

## Hints
```
{hints}
```
