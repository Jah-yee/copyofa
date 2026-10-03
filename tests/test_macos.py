from pathlib import Path

import pytest

from conftest import FIXTURES
from copyofa.errors import EnvironmentFailure
from copyofa.system import macos

PRODUCT = "CD-R   PX-W5224A"


def status_xml() -> bytes:
    return (FIXTURES / "drutil" / "8W20X-status.xml").read_bytes()


def test_drutil_fixture_descriptor() -> None:
    status = macos.parse_drutil_status(status_xml(), PRODUCT)
    assert status is not None
    assert status.present
    assert not status.in_transition
    medium = status.descriptor()
    assert (medium.family, medium.media_type, medium.sessions, medium.disc_status) == (
        "CD",
        "CD-ROM",
        1,
        2,
    )
    assert not medium.blank
    assert not medium.recordable
    assert not medium.rewritable
    assert not medium.data_tracks
    assert len(medium.tracks) == 14
    assert medium.tracks[-1] == macos.TrackInfo(14, 1, 0, 272116, 6676)


def test_other_product_is_ignored() -> None:
    assert macos.parse_drutil_status(status_xml(), "OTHER") is None


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (b"<burnSpeedList/>", b"<burnSpeedList/><blank/>"),
        (b'<discStatus value="2"', b'<discStatus value="0"'),
    ],
)
def test_blank_detection(old: bytes, new: bytes) -> None:
    status = macos.parse_drutil_status(status_xml().replace(old, new), PRODUCT)
    assert status is not None
    assert status.descriptor().blank


def test_recordable_types() -> None:
    xml = status_xml().replace(
        b'<mediaType value="CD-ROM"/>', b'<mediaType value="CD-RW"/>'
    )
    status = macos.parse_drutil_status(xml, PRODUCT)
    assert status is not None
    medium = status.descriptor()
    assert medium.recordable
    assert medium.rewritable
    xml = status_xml().replace(
        b'<mediaClass value="CD"/>', b'<mediaClass value="Unknown"/>'
    )
    status = macos.parse_drutil_status(xml, PRODUCT)
    assert status is not None
    assert status.descriptor().family == "unknown"


def test_mount_parsing() -> None:
    text = (
        "/dev/disk3s1 on / (apfs, sealed, local, read-only, journaled)\n"
        "/dev/disk4 on /Volumes/Audio CD (cddafs, local, nodev, nosuid, "
        "read-only, noowners)\n"
        "/dev/disk40s2 on /Volumes/X (hfs, local)\n"
        "/dev/disk4s2 on /Volumes/Data (hfs, local, nodev)\n"
    )
    mounts = macos.parse_mounts(text, "disk4")
    assert mounts == [
        macos.Mount("/dev/disk4", "/Volumes/Audio CD", "cddafs", True),
        macos.Mount("/dev/disk4s2", "/Volumes/Data", "hfs", False),
    ]


def test_drutil_verbs_restricted() -> None:
    assert macos.drutil_argv(PRODUCT, "status") == [
        "drutil",
        "-drive",
        PRODUCT,
        "status",
        "-xml",
    ]
    for verb in ("eject", "tray", "burn", "erase"):
        with pytest.raises(ValueError, match="is not allowed"):
            macos.drutil(PRODUCT, verb)


def test_description_keys_match_diskarbitration() -> None:
    import DiskArbitration

    assert DiskArbitration.kDADiskDescriptionDeviceVendorKey == "DADeviceVendor"
    assert DiskArbitration.kDADiskDescriptionDeviceModelKey == "DADeviceModel"
    assert DiskArbitration.kDADiskDescriptionMediaBSDNameKey == "DAMediaBSDName"


def test_predicates() -> None:
    from copyofa.config import DriveConfig

    drive = DriveConfig("plextor", "PLEXTOR", PRODUCT, "1.04", ("CD",))
    predicate = macos.drive_predicate([drive])
    assert predicate({"DADeviceVendor": "PLEXTOR ", "DADeviceModel": "CD-R PX-W5224A"})
    assert not predicate({"DADeviceVendor": "APPLE", "DADeviceModel": "SSD"})
    assert macos.bsd_predicate({"disk9"})({"DAMediaBSDName": "disk9s1"})


def test_file_lock_is_exclusive(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    with (
        macos.FileLock(path),
        pytest.raises(EnvironmentFailure, match="held by another copyofa process"),
        macos.FileLock(path),
    ):
        pass
    with macos.FileLock(path):
        pass
