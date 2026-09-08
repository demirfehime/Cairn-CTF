from __future__ import annotations

import logging
import codecs
import io
from pathlib import Path
import os
import signal
import shutil
import subprocess
import threading
from contextlib import suppress

from cairn.dispatcher.runtime.process import ProcessResult

LOG = logging.getLogger(__name__)

READ_CHUNK_SIZE = 65536
STREAM_JOIN_TIMEOUT_SECONDS = 5.0
FORCE_KILL_REAP_TIMEOUT_SECONDS = 2.0


class LocalProcess:
    """Runs a worker command as a host subprocess.

    Mirrors the container ManagedProcess surface (start/communicate/kill/cancel) but
    executes on the dispatcher host: its own process group so children are killed as a
    group, a Python-enforced timeout instead of the ``timeout`` coreutil, and a
    SIGTERM -> grace -> SIGKILL shutdown so the CLI can flush its session before dying.
    """

    def __init__(
        self,
        command: list[str],
        cwd: str,
        env: dict[str, str],
        timeout_seconds: int | None = None,
        term_grace_seconds: int = 5,
        stdin_text: str | None = None,
        log_dir: Path | None = None,
    ):
        self.log_dir = log_dir
        self.command = command
        self.env = env
        self._cwd = cwd
        self._timeout_seconds = timeout_seconds
        self._term_grace = max(1.0, float(term_grace_seconds))
        self._stdin_text = stdin_text
        self._process: subprocess.Popen[str] | None = None
        self._stdout_chunks: list[str] = []
        self._stderr_chunks: list[str] = []
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._timed_out = False
        self._cancel_reason: str | None = None
        self._kill_lock = threading.Lock()

    def start(self) -> None:
        if self.log_dir is not None:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        command = list(self.command)
        executable = shutil.which(command[0])
        if executable is not None:
            # Windows cannot CreateProcess an npm-generated ``foo.cmd`` by its bare
            # command name, even though ``shutil.which`` can find it.  Passing the
            # resolved path works and is harmless for native executables on POSIX.
            command[0] = executable

        process_group_options: dict[str, object]
        if os.name == "nt":
            # ``taskkill /T`` below can terminate the full descendant tree by PID.
            # Avoid CREATE_NEW_PROCESS_GROUP here: it can fail in non-interactive
            # Windows sessions (WinError 1312) and is not needed for taskkill.
            process_group_options = {}
        else:
            process_group_options = {"start_new_session": True}

        self._process = subprocess.Popen(
            command,
            cwd=self._cwd,
            env=self.env,
            stdin=subprocess.PIPE if self._stdin_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **process_group_options,
        )
        self._stdout_thread = threading.Thread(
            target=self._drain, args=(self._process.stdout, self._stdout_chunks, self.log_dir / "stdout.log" if self.log_dir else None), daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._drain, args=(self._process.stderr, self._stderr_chunks, self.log_dir / "stderr.log" if self.log_dir else None), daemon=True
        )
        self._stdout_thread.start()
        self._stderr_thread.start()
        if self._stdin_text is not None and self._process.stdin is not None:
            try:
                self._process.stdin.write(self._stdin_text)
                self._process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                with suppress(OSError, ValueError):
                    self._process.stdin.close()

    def communicate(self, timeout: float | None) -> ProcessResult:
        assert self._process is not None
        wait_for = float(self._timeout_seconds) if self._timeout_seconds is not None else timeout
        try:
            self._process.wait(timeout=wait_for)
        except subprocess.TimeoutExpired:
            self._timed_out = True
            self._terminate()
        with suppress(subprocess.TimeoutExpired):
            self._process.wait(timeout=FORCE_KILL_REAP_TIMEOUT_SECONDS)
        if self._stdout_thread is not None:
            self._stdout_thread.join(timeout=STREAM_JOIN_TIMEOUT_SECONDS)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=STREAM_JOIN_TIMEOUT_SECONDS)
        returncode = self._process.returncode
        if returncode is None:
            returncode = 137 if self._timed_out else 1
        return ProcessResult(
            returncode=returncode,
            stdout="".join(self._stdout_chunks),
            stderr="".join(self._stderr_chunks),
            timed_out=self._timed_out,
            cancelled=self._cancel_reason is not None,
            cancel_reason=self._cancel_reason,
        )

    def kill(self) -> None:
        self._terminate()

    def cancel(self, reason: str) -> None:
        if self._cancel_reason is None:
            self._cancel_reason = reason
        self._terminate()

    def _terminate(self) -> None:
        with self._kill_lock:
            process = self._process
            if process is None or process.poll() is not None:
                return
            if os.name == "nt":
                self._terminate_windows_tree(process)
                return
            self._signal_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=self._term_grace)
                return
            except subprocess.TimeoutExpired:
                pass
            self._signal_group(process, signal.SIGKILL)

    def _terminate_windows_tree(self, process: subprocess.Popen[str]) -> None:
        """Terminate the worker and descendants without relying on POSIX process groups."""
        self._run_taskkill(process.pid, force=False)
        try:
            process.wait(timeout=self._term_grace)
            return
        except subprocess.TimeoutExpired:
            pass
        self._run_taskkill(process.pid, force=True)

    @staticmethod
    def _run_taskkill(pid: int, *, force: bool) -> None:
        command = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            command.append("/F")
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )

    @staticmethod
    def _signal_group(process: subprocess.Popen[str], sig: int) -> None:
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (ProcessLookupError, PermissionError):
            with suppress(ProcessLookupError, PermissionError, ValueError):
                process.send_signal(sig)

    @staticmethod
    def _drain(pipe, sink: list[str], log_path: Path | None = None) -> None:
        handle = None
        try:
            if log_path is not None:
                try:
                    handle = log_path.open("a", encoding="utf-8")
                except OSError:
                    LOG.exception("Cannot open worker log: %s", log_path)
            # read1 returns available bytes, so short output is saved while the
            # worker is still running. Preserve split UTF-8 characters.
            decoder = io.IncrementalNewlineDecoder(codecs.getincrementaldecoder("utf-8")("replace"), translate=True)
            while True:
                data = pipe.buffer.read1(READ_CHUNK_SIZE)
                chunk = decoder.decode(data, final=not data)
                if chunk:
                    sink.append(chunk)
                    if handle is not None:
                        try:
                            handle.write(chunk)
                            handle.flush()
                        except OSError:
                            LOG.exception("Cannot persist worker output: %s", log_path)
                            handle.close()
                            handle = None
                if not data:
                    break
        except (ValueError, OSError):
            LOG.exception("Cannot read or save worker output: %s", log_path)
        finally:
            if handle is not None:
                handle.close()
            with suppress(Exception):
                pipe.close()
