"""redumper command construction with an explicit allow-list."""

import re
from pathlib import Path

from ..system.procs import run_capture

DRIVE_COMMANDS = {"dump", "dump::extra", "protection", "refine", "drive::test"}
OFFLINE_COMMANDS = {"split", "hash", "info"}


def _refine_option(option: str) -> bool:
    if option == "--refine-subchannel":
        return True
    if match := re.fullmatch(r"--retries=(\d+)", option):
        return int(match.group(1)) >= 0
    if match := re.fullmatch(r"--speed=(\d+(?:\.\d+)?)", option):
        return float(match.group(1)) > 0
    return False


def _check_options(command: str, options: tuple[str, ...] | list[str]) -> None:
    for option in options:
        allowed = {
            "dump": option == "--overwrite",
            "refine": _refine_option(option),
            "split": option in ("--leave-unchanged", "--force-split"),
        }.get(command, False)
        if not allowed:
            raise ValueError(
                f"redumper option {option!r} is not allowed for {command!r}"
            )
    if command == "split" and "--leave-unchanged" not in options:
        raise ValueError("redumper split must run with --leave-unchanged")


def build_argv(
    redumper: str,
    command: str,
    *,
    image_path: Path | str,
    image_name: str,
    drive_bsd: str | None = None,
    options: tuple[str, ...] | list[str] = (),
) -> list[str]:
    """Return a redumper argv; raises ValueError for anything outside the allow-list."""
    if command not in DRIVE_COMMANDS | OFFLINE_COMMANDS:
        raise ValueError(f"redumper command {command!r} is not allowed")
    _check_options(command, tuple(options))
    argv = [
        redumper,
        command,
        "--verbose",
        f"--image-path={image_path}",
        f"--image-name={image_name}",
    ]
    if command in DRIVE_COMMANDS:
        if not drive_bsd:
            raise ValueError(f"redumper {command} needs a drive")
        argv.append(f"--drive={drive_bsd}")
    elif drive_bsd:
        raise ValueError(f"redumper {command} must not access the drive")
    argv.extend(options)
    return argv


def redumper_build(redumper: str) -> str | None:
    """Build string printed by `redumper --version`, e.g. `b753+d8fix`."""
    code, out = run_capture([redumper, "--version"])
    match = re.search(r"redumper \(build: (\S+)\)", out.decode("utf-8", "replace"))
    return match.group(1) if code == 0 and match else None
