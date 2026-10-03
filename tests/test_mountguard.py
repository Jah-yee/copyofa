import re
import subprocess
from pathlib import Path

import pytest

from copyofa.system.macos import MountGuard, bsd_predicate

pytestmark = pytest.mark.macos_integration


def run(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def test_mount_refused_while_guard_active(tmp_path: Path) -> None:
    image = tmp_path / "t.dmg"
    created = run(
        "hdiutil",
        "create",
        "-size",
        "8m",
        "-fs",
        "HFS+",
        "-layout",
        "NONE",
        "-volname",
        "COPYOFATEST",
        str(image),
    )
    if created.returncode:
        pytest.skip(f"hdiutil create failed: {created.stderr}")
    attached = run("hdiutil", "attach", "-nomount", str(image))
    if attached.returncode:
        pytest.skip(f"hdiutil attach failed: {attached.stderr}")
    match = re.search(r"/dev/(disk\d+)", attached.stdout)
    assert match is not None
    disk = match.group(1)
    try:
        with MountGuard(bsd_predicate({disk})) as guard:
            refused = run("diskutil", "mount", disk)
            assert refused.returncode != 0
            assert disk in guard.refused
        allowed = run("diskutil", "mount", disk)
        assert allowed.returncode == 0, allowed.stderr
    finally:
        run("hdiutil", "detach", "-force", f"/dev/{disk}")
