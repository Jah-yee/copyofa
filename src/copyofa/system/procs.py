"""Recorded subprocess runs (session records)."""

import os
import shlex
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..util import utc_now


@dataclass(frozen=True)
class StageRun:
    argv: list[str]
    command_line: str
    started_at: str
    ended_at: str
    exit_code: int
    interrupted: bool
    record_path: Path


def run_recorded(argv: list[str], record_path: Path, *, cwd: Path | None = None, echo: bool = True) -> StageRun:
    """Run argv with stderr merged into stdout; store every byte in record_path (created, never replaced)."""
    started_at = utc_now()
    interrupted = False

    def forward_interrupt() -> None:
        nonlocal interrupted
        if not interrupted and proc.poll() is None:
            proc.send_signal(signal.SIGINT)  # redumper handles SIGINT and exits cleanly
        interrupted = True

    with open(record_path, "xb") as record:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd)
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except KeyboardInterrupt:
                forward_interrupt()
                continue
            if not chunk:
                break
            record.write(chunk)
            record.flush()  # a killed coa still leaves everything read so far in the record
            if echo:
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
        while True:
            try:
                exit_code = proc.wait()
                break
            except KeyboardInterrupt:
                forward_interrupt()
        proc.stdout.close()
        record.flush()
        os.fsync(record.fileno())
    os.chmod(record_path, 0o444)
    return StageRun(
        argv=list(argv),
        command_line=shlex.join(argv),
        started_at=started_at,
        ended_at=utc_now(),
        exit_code=exit_code,
        interrupted=interrupted,
        record_path=record_path,
    )


def run_capture(argv: list[str], *, cwd: Path | None = None) -> tuple[int, bytes]:
    """Run an unrecorded probe; returns (exit code, combined output)."""
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd, check=False)
    except FileNotFoundError:
        return 127, b""
    return proc.returncode, proc.stdout
