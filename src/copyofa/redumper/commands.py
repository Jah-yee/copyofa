"""redumper command construction with an explicit command allow-list, build
parsing, and which drives must capture the lead-in."""

import re
from dataclasses import dataclass
from pathlib import Path

from ..system.procs import run_capture

DRIVE_COMMANDS = {"dump", "dump::extra", "protection", "refine", "drive::test"}
OFFLINE_COMMANDS = {"split", "hash", "info"}
# Oldest build with every command, option and console format copyofa relies on.
MIN_BUILD = 753
# redumper reads the lead-in only on drives of this vendor.
LEADIN_VENDOR = "PLEXTOR"


def leadin_expected(vendor: str | None) -> bool:
    """Whether a missing lead-in must block: true for PLEXTOR drives, and for a
    drive whose vendor is unknown so that unreadable identity never waives it."""
    return not vendor or vendor.strip() == LEADIN_VENDOR


@dataclass(frozen=True)
class Build:
    """A redumper build string: `b756` upstream, `b756+d8fix` with a local patch."""

    number: int
    patches: frozenset[str]


def parse_build(text: str) -> Build | None:
    match = re.fullmatch(r"b(\d+)((?:\+[^+\s]+)*)", text)
    if match is None:
        return None
    return Build(int(match.group(1)), frozenset(match.group(2).split("+")[1:]))


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
    """Build string printed by `redumper --version`, e.g. `b756+d8fix`."""
    code, out = run_capture([redumper, "--version"])
    match = re.search(r"redumper \(build: (\S+)\)", out.decode("utf-8", "replace"))
    return match.group(1) if code == 0 and match else None
