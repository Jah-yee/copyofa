"""Minimum redumper build, known-bad bridge/build combinations and the optional
firmware pin."""

import dataclasses
from pathlib import Path

import pytest

from conftest import FIXTURES
from copyofa import config
from copyofa.acquisition.acquire import (
    Selected,
    check_firmware,
    check_known_bad,
    environment_preflight,
)
from copyofa.config import Config, DriveConfig, KnownBad
from copyofa.errors import EnvironmentFailure
from copyofa.system import macos

PRODUCT = "CD-R   PX-W5224A"
JMICRON = macos.Bridge("0x152D", "0x2338", "USB to ATA/ATAPI bridge", None)


def make_config(tmp_path: Path) -> Config:
    path = tmp_path / "config.toml"
    config.init(path, tmp_path / "archive", tmp_path / "work")
    return dataclasses.replace(
        config.load(str(path)),
        known_bad=(KnownBad("0x152d:0x2338", ("d8fix",), 760, "pads transfers"),),
    )


def test_min_build_cannot_go_below_supported(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config.init(path, tmp_path / "archive", tmp_path / "work")
    text = path.read_text()
    path.write_text(text.replace("min_build = 753", "min_build = 752"))
    with pytest.raises(EnvironmentFailure, match="below b753"):
        config.load(str(path))


@pytest.mark.parametrize(("build", "refused"), [("b754+d8fix", True), ("b755", False)])
def test_preflight_enforces_min_build(
    tmp_path: Path, build: str, refused: bool
) -> None:
    redumper = tmp_path / "redumper"
    redumper.write_text(f'#!/bin/sh\necho "redumper (build: {build})"\n')
    redumper.chmod(0o755)
    cfg = dataclasses.replace(
        make_config(tmp_path),
        redumper_path=str(redumper),
        min_build=755,
        min_free_work_bytes=0,
    )
    if refused:
        with pytest.raises(EnvironmentFailure, match="b755 or later"):
            environment_preflight(cfg)
    else:
        assert environment_preflight(cfg) == (str(redumper), build)


def location(
    bridge: macos.Bridge | None, firmware: str = "1.04"
) -> macos.DriveLocation:
    return macos.DriveLocation("PLEXTOR", PRODUCT, firmware, {}, "disk4", bridge, "USB")


@pytest.mark.parametrize(
    ("build", "refused"),
    [
        ("b756", True),
        ("b759+other", True),
        ("b756+d8fix", False),
        ("b756+other+d8fix", False),
        ("b760", False),
    ],
)
def test_known_bad_until_fixed(tmp_path: Path, build: str, refused: bool) -> None:
    cfg = make_config(tmp_path)
    if refused:
        with pytest.raises(EnvironmentFailure, match="known-bad combination"):
            check_known_bad(cfg, location(JMICRON), build)
    else:
        check_known_bad(cfg, location(JMICRON), build)


def test_known_bad_needs_the_listed_bridge(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    check_known_bad(cfg, location(None), "b756")
    check_known_bad(cfg, location(macos.Bridge("0x174C", "0x55AA", None, None)), "b756")


def selected(configured: str | None, reported: str) -> Selected:
    xml = (FIXTURES / "drutil" / "8W20X-status.xml").read_bytes()
    status = macos.parse_drutil_status(xml, PRODUCT)
    assert status is not None
    drive = DriveConfig("plextor", "PLEXTOR", PRODUCT, configured, ("CD",))
    return Selected(drive, location(None, reported), status, "USB")


def test_firmware_pinned_only_when_configured() -> None:
    check_firmware(selected(None, "1.06"))
    check_firmware(selected("1.04", "1.04"))
    with pytest.raises(EnvironmentFailure, match=r"reports firmware 1\.06"):
        check_firmware(selected("1.04", "1.06"))
