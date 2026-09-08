# Automatic log retention

Runtime logs are retained under `.cairn-launcher/logs/<startup-time>/`.
Launcher output is appended, and startup folders have subsecond timestamps.
Child dispatcher logs are appended under `.cairn-launcher/runs/<project>/.cairn-child/logs/`.

Agent stdout and stderr are automatically flushed while running to:

`.cairn-launcher/logs/workers/<project-workspace>/<execution-time-and-id>/stdout.log`

`.cairn-launcher/logs/workers/<project-workspace>/<execution-time-and-id>/stderr.log`

Each execution has a separate folder. Worker archives are outside the project workspace;
completion cleanup, task retries and service restarts do not overwrite or remove them.
For a custom workspace root, worker logs are stored in its sibling `logs/workers` directory.

The project logs API (`GET /projects/{id}/logs`) lists retained runtime runs,
child logs and worker output. Historical runtime links include a fixed run identifier,
so they still point to the same file after restarting. Pentest's Logs dialog uses this list.
CTF also exposes the logs API; all files can be opened directly from the folders above.

No retention limit or automatic deletion is applied to these archives.
Previously unsaved in-memory Agent output cannot be recovered by this change.
