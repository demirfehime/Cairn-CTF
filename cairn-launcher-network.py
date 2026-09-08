"""Reject local service startup when its configured API sockets are forbidden."""
from __future__ import annotations

import errno
import socket
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlsplit


def check_network(database: Path) -> int:
    if not database.is_file():
        return 0
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='agents'").fetchone():
            return 0
        urls = [row[0] for row in conn.execute(
            "SELECT DISTINCT base_url FROM agents WHERE enabled = 1"
        )]
    endpoints = set()
    for url in urls:
        parsed = urlsplit(url or "")
        if parsed.scheme in {"https", "http"} and parsed.hostname:
            endpoints.add((parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)))
    for host, port in sorted(endpoints):
        try:
            with socket.create_connection((host, port), timeout=3):
                pass
        except OSError as exc:
            if getattr(exc, "winerror", None) == 10013 or exc.errno in {errno.EACCES, errno.EPERM, 10013}:
                print(f"Network permission denied for {host}:{port}. "
                      "Start Cairn from a normal Windows terminal or an explicitly approved "
                      "network-enabled execution context. Existing services have not been stopped.")
                return 1
            # An offline gateway must not prevent access to the local settings UI.
            print(f"Warning: {host}:{port} is unreachable ({type(exc).__name__}); "
                  "check gateway connectivity in Agent settings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(check_network(Path(sys.argv[1])))
