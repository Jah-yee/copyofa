from pathlib import Path

import pytest

from copyofa import config, vocabulary
from copyofa.errors import EnvironmentFailure, UsageError
from copyofa.ids import (
    MediumId,
    normalize_holon_id,
    parse_medium_id,
    validate_media_list,
)

VOCAB = vocabulary.parse(vocabulary.default_text(), Path("media-prefixes.toml"))


@pytest.mark.parametrize(
    "holon", ["T02PQ", "8W20X", "CAW20", "JJ314", "KGWHD", "08T2J", "7PSQ4", "W842C"]
)
def test_valid_holon_ids(holon: str) -> None:
    assert normalize_holon_id(holon) == holon


@pytest.mark.parametrize("holon", ["T02PR", "T20PQ"])
def test_check_character_catches_substitution_and_swap(holon: str) -> None:
    with pytest.raises(UsageError, match="check character"):
        normalize_holon_id(holon)


@pytest.mark.parametrize("text", ["t02-pq", "TO2PQ", " t02 pq "])
def test_holon_normalization(text: str) -> None:
    assert normalize_holon_id(text) == "T02PQ"


def test_holon_alphabet_and_length() -> None:
    with pytest.raises(UsageError, match="alphabet"):
        normalize_holon_id("T02PU")
    with pytest.raises(UsageError, match="5 characters"):
        normalize_holon_id("T02P")


def test_medium_ids() -> None:
    assert parse_medium_id("CD1") == MediumId("CD", 1, "")
    assert str(parse_medium_id("cd12")) == "CD12"
    assert parse_medium_id("DD1A") == MediumId("DD", 1, "A")
    for bad in ("CD01", "CD0", "1CD", "CD"):
        with pytest.raises(UsageError, match="never zero-padded"):
            parse_medium_id(bad)


def test_media_lists() -> None:
    assert [str(m) for m in validate_media_list("DD1A,DD1B", VOCAB)] == ["DD1A", "DD1B"]
    assert [str(m) for m in validate_media_list("DVD2A, DVD2B CD1", VOCAB)] == [
        "DVD2A",
        "DVD2B",
        "CD1",
    ]
    assert [str(m) for m in validate_media_list("CD1,VINYL1A,VINYL1B", VOCAB)] == [
        "CD1",
        "VINYL1A",
        "VINYL1B",
    ]
    for bad, rule in [
        ("DD1A", "both sides"),
        ("DD1", "requires a side"),
        ("CD1A", "does not take a side"),
        ("CD1,CD1", "twice"),
        ("XX1", "unknown medium prefix"),
        ("DVD1,DVD1A,DVD1B", "with and without"),
        ("", "empty"),
    ]:
        with pytest.raises(UsageError, match=rule):
            validate_media_list(bad, VOCAB)


def test_init_writes_config_and_vocabulary(tmp_path: Path) -> None:
    cfg_path = tmp_path / "cfg" / "config.toml"
    config.init(cfg_path, tmp_path / "archive", tmp_path / "work")
    cfg = config.load(str(cfg_path))
    assert cfg.archive_root == (tmp_path / "archive").resolve()
    assert cfg.allowed_builds == ("b753+d8fix",)
    assert cfg.drives[0].product == "CD-R   PX-W5224A"
    assert vocabulary.load(cfg.archive_root).prefixes["SACD"].partial
    with pytest.raises(UsageError, match="already exists"):
        config.init(cfg_path, tmp_path / "archive", tmp_path / "work")


def test_work_root_in_private_folder_rejected(tmp_path: Path) -> None:
    with pytest.raises(EnvironmentFailure, match="privacy"):
        config.check_private_folder(Path.home() / "Documents" / "coa-work")
    cfg = tmp_path / "config.toml"
    cfg.write_text('archive_root = "/tmp/a"\n')
    with pytest.raises(EnvironmentFailure, match="work_root"):
        config.load(str(cfg))
