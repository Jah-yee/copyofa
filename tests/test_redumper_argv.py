import re
import shutil
import sys
from pathlib import Path

import pytest

from copyofa.redumper.commands import (
    MIN_BUILD,
    Build,
    build_argv,
    leadin_expected,
    parse_build,
    redumper_build,
)
from copyofa.system.procs import run_recorded


def test_allowed_commands() -> None:
    argv = build_argv(
        "redumper",
        "dump",
        image_path="/w/dump",
        image_name="8W20X-CD1",
        drive_bsd="disk4",
    )
    assert argv == [
        "redumper",
        "dump",
        "--verbose",
        "--image-path=/w/dump",
        "--image-name=8W20X-CD1",
        "--drive=disk4",
    ]
    assert build_argv(
        "r",
        "refine",
        image_path="p",
        image_name="n",
        drive_bsd="disk4",
        options=["--retries=30", "--refine-subchannel", "--speed=8"],
    )[-3:] == ["--retries=30", "--refine-subchannel", "--speed=8"]
    assert build_argv(
        "r",
        "split",
        image_path="p",
        image_name="n",
        options=["--leave-unchanged", "--force-split"],
    )
    assert build_argv(
        "r",
        "dump",
        image_path="p",
        image_name="n",
        drive_bsd="d",
        options=["--overwrite"],
    )
    for command in ("hash", "info"):
        assert "--drive" not in " ".join(
            build_argv("r", command, image_path="p", image_name="n")
        )


@pytest.mark.parametrize(
    ("command", "options", "drive", "error"),
    [
        ("disc", (), "disk4", "redumper command 'disc' is not allowed"),
        ("eject", (), "disk4", "redumper command 'eject' is not allowed"),
        (
            "flash::plextor",
            (),
            "disk4",
            "redumper command 'flash::plextor' is not allowed",
        ),
        (
            "split",
            ("--leave-unchanged", "--filesystem-trim"),
            None,
            "redumper option '--filesystem-trim' is not allowed for 'split'",
        ),
        (
            "dump::extra",
            ("--plextor-leadin-force-store",),
            "disk4",
            (
                "redumper option '--plextor-leadin-force-store' is not allowed for "
                "'dump::extra'"
            ),
        ),
        (
            "split",
            ("--leave-unchanged", "--overwrite"),
            None,
            "redumper option '--overwrite' is not allowed for 'split'",
        ),
        ("split", (), None, "redumper split must run with --leave-unchanged"),
        (
            "refine",
            ("--retries=-1",),
            "disk4",
            "redumper option '--retries=-1' is not allowed for 'refine'",
        ),
        (
            "refine",
            ("--speed=0",),
            "disk4",
            "redumper option '--speed=0' is not allowed for 'refine'",
        ),
        (
            "split",
            ("--leave-unchanged",),
            "disk4",
            "redumper split must not access the drive",
        ),
        ("dump", (), None, "redumper dump needs a drive"),
    ],
)
def test_rejected(
    command: str, options: tuple[str, ...], drive: str | None, error: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(error)):
        build_argv(
            "redumper",
            command,
            image_path="p",
            image_name="n",
            drive_bsd=drive,
            options=options,
        )


def test_run_recorded_keeps_bytes(tmp_path: Path) -> None:
    record = tmp_path / "x.console"
    script = (
        "import sys; sys.stdout.write('a\\r' + ' ' * 79 + '\\rb\\n'); "
        "sys.stderr.write('err\\n')"
    )
    # -u: without it the piped stdout is block-buffered and stderr's line lands first.
    run = run_recorded([sys.executable, "-u", "-c", script], record, echo=False)
    assert run.exit_code == 0
    assert not run.interrupted
    assert record.read_bytes().startswith(b"a\r" + b" " * 79 + b"\rb\n")
    assert b"err\n" in record.read_bytes()
    assert record.stat().st_mode & 0o777 == 0o444
    with pytest.raises(FileExistsError):
        run_recorded([sys.executable, "-c", "pass"], record, echo=False)


def test_run_recorded_forwards_interrupt(tmp_path: Path) -> None:
    import signal
    import subprocess
    import time

    child = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGINT, "
        "lambda *a: (print('child got SIGINT', flush=True), sys.exit(3)))\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )
    parent = (
        "import sys; from pathlib import Path; "
        "from copyofa.system.procs import run_recorded\n"
        f"r = run_recorded([sys.executable, '-c', {child!r}], "
        f"Path({str(tmp_path / 'r.console')!r}), echo=False)\n"
        "print(r.exit_code, r.interrupted, flush=True)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", parent],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    record = tmp_path / "r.console"
    deadline = time.monotonic() + 10
    while (
        not (record.exists() and b"ready" in record.read_bytes())
        and time.monotonic() < deadline
    ):
        time.sleep(0.05)
    proc.send_signal(
        signal.SIGINT
    )  # only the parent: the child sees SIGINT only if it is forwarded
    out, _ = proc.communicate(timeout=10)
    assert out.strip() == "3 True"
    assert b"child got SIGINT" in record.read_bytes()


def test_parse_build() -> None:
    assert parse_build("b753") == Build(753, frozenset())
    assert parse_build("b756+d8fix") == Build(756, frozenset({"d8fix"}))
    assert parse_build("b756+a+b") == Build(756, frozenset({"a", "b"}))
    for bad in ("753", "b", "bX", "b756+", "b756 ", "v756"):
        assert parse_build(bad) is None, bad


def test_missing_leadin_waived_only_for_known_other_vendors() -> None:
    assert leadin_expected("PLEXTOR")
    assert leadin_expected("PLEXTOR ")
    assert leadin_expected(None)
    assert leadin_expected("")
    assert not leadin_expected("ASUS")


@pytest.mark.skipif(not shutil.which("redumper"), reason="redumper not installed")
def test_installed_redumper_is_supported() -> None:
    build = redumper_build("redumper")
    assert build is not None
    parsed = parse_build(build)
    assert parsed is not None, build
    assert parsed.number >= MIN_BUILD
