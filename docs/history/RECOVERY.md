# Cairn 2026-07-19 Afternoon Recovery

This directory is a side-by-side reconstruction of the Cairn source used by
`proj_055` on 2026-07-19 (Asia/Shanghai).

## Identity

- Git base: `8f702c5f3f9d3163948bd4089edc73980c9c9484`
- Observed launcher instance: `20260719-170112`
- `proj_055` first Parent dispatch: 2026-07-19 17:55:09 +08:00
- `proj_055` completion: 2026-07-19 18:14:35 +08:00
- Source snapshot read by Codex: 2026-07-19 23:41-23:44 +08:00
- First recorded later source patch: 2026-07-20 11:50:43 +08:00

## Recovery method

The 2026-07-17 full source backup was used as the filesystem baseline. Runtime
Python modules, dispatcher prompts, configuration examples, launcher scripts,
and orchestration modules were then overlaid from the complete file contents
captured in the 2026-07-19 Codex session. Later recorded UI patches were
reversed. The restored UI matches the captured first 180 lines and the exact
line-number/name index of all 210 JavaScript functions.

## Verification

- All recovered Python source compiles successfully with `compileall`.
- `cairn.server.app`, `DispatcherLoop`, and `ParentOrchestrator` import successfully.
- The active Cairn directory and its running service were not changed.

## Scope note

The runtime source and UI are reconstructed from the 2026-07-19 evidence. Test
files not captured verbatim that night remain from the 2026-07-17 source
backup, so this directory is suitable for runtime reproduction and behavioral
comparison but is not represented as an original Git commit.
