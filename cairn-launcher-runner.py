"""Small detached-process runner used by the Windows PowerShell launcher."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    spec_path = Path(sys.argv[1])
    with spec_path.open("r", encoding="utf-8-sig") as handle:
        spec = json.load(handle)

    stdout_path = Path(spec["stdout"])
    stderr_path = Path(spec["stderr"])
    pid_path = Path(spec["pid_file"])
    stdout_path.parent.mkdir(parents=True, exist_ok=True)

    with stdout_path.open("a", encoding="utf-8", errors="replace") as stdout, stderr_path.open(
        "a", encoding="utf-8", errors="replace"
    ) as stderr:
        process = subprocess.Popen(
            [spec["executable"], *spec["arguments"]],
            cwd=spec["cwd"],
            stdout=stdout,
            stderr=stderr,
        )
        pid_path.write_text(str(process.pid), encoding="ascii")
        return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
