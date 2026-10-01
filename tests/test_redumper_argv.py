import shutil
import sys

import pytest

from copyofa.redumper.commands import build_argv, redumper_build
from copyofa.system.procs import run_recorded


def test_allowed_commands():
    argv = build_argv("redumper", "dump", image_path="/w/dump", image_name="8W20X-CD1", drive_bsd="disk4")
    assert argv == [
        "redumper", "dump", "--verbose", "--image-path=/w/dump", "--image-name=8W20X-CD1", "--drive=disk4",
    ]
    assert build_argv(
        "r", "refine", image_path="p", image_name="n", drive_bsd="disk4",
        options=["--retries=30", "--refine-subchannel", "--speed=8"],
    )[-3:] == ["--retries=30", "--refine-subchannel", "--speed=8"]
    assert build_argv("r", "split", image_path="p", image_name="n", options=["--leave-unchanged", "--force-split"])
    assert build_argv("r", "dump", image_path="p", image_name="n", drive_bsd="d", options=["--overwrite"])
    for command in ("hash", "info"):
        assert "--drive" not in " ".join(build_argv("r", command, image_path="p", image_name="n"))


@pytest.mark.parametrize(
    "command,options,drive",
    [
        ("disc", (), "disk4"),
        ("eject", (), "disk4"),
        ("flash::plextor", (), "disk4"),
        ("split", ("--leave-unchanged", "--filesystem-trim"), None),
        ("dump::extra", ("--plextor-leadin-force-store",), "disk4"),
        ("split", ("--leave-unchanged", "--overwrite"), None),
        ("split", (), None),
        ("refine", ("--retries=-1",), "disk4"),
        ("refine", ("--speed=0",), "disk4"),
        ("split", ("--leave-unchanged",), "disk4"),
        ("dump", (), None),
    ],
)
def test_rejected(command, options, drive):
    with pytest.raises(ValueError):
        build_argv("redumper", command, image_path="p", image_name="n", drive_bsd=drive, options=options)


def test_run_recorded_keeps_bytes(tmp_path):
    record = tmp_path / "x.console"
    script = "import sys; sys.stdout.write('a\\r' + ' ' * 79 + '\\rb\\n'); sys.stderr.write('err\\n')"
    run = run_recorded([sys.executable, "-c", script], record, echo=False)
    assert run.exit_code == 0 and not run.interrupted
    assert record.read_bytes().startswith(b"a\r" + b" " * 79 + b"\rb\n")
    assert b"err\n" in record.read_bytes()
    assert record.stat().st_mode & 0o777 == 0o444
    with pytest.raises(FileExistsError):
        run_recorded([sys.executable, "-c", "pass"], record, echo=False)


def test_run_recorded_forwards_interrupt(tmp_path):
    import signal
    import subprocess
    import time

    child = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGINT, lambda *a: (print('child got SIGINT', flush=True), sys.exit(3)))\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )
    parent = (
        "import sys; from pathlib import Path; from copyofa.system.procs import run_recorded\n"
        f"r = run_recorded([sys.executable, '-c', {child!r}], Path({str(tmp_path / 'r.console')!r}), echo=False)\n"
        "print(r.exit_code, r.interrupted, flush=True)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", parent], stdout=subprocess.PIPE, text=True,
                            start_new_session=True)
    record = tmp_path / "r.console"
    deadline = time.monotonic() + 10
    while not (record.exists() and b"ready" in record.read_bytes()) and time.monotonic() < deadline:
        time.sleep(0.05)
    proc.send_signal(signal.SIGINT)  # only the parent: the child sees SIGINT only if it is forwarded
    out, _ = proc.communicate(timeout=10)
    assert out.strip() == "3 True"
    assert b"child got SIGINT" in record.read_bytes()


@pytest.mark.skipif(not shutil.which("redumper"), reason="redumper not installed")
def test_redumper_build():
    assert redumper_build("redumper") == "b753+d8fix"
