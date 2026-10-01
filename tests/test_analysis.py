from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

import replay
from conftest import pilot_dir
from copyofa.archive import metadata
from copyofa.archive.analysis import DERIVED_KEYS, derive
from copyofa.redumper.messages import Catalog

pytestmark = pytest.mark.pilot


def statuses(derived: dict[str, Any]) -> dict[str, Any]:
    return {c["id"]: c["status"] for c in derived["checks"]}


def test_8w20x_derived_fields(tmp_path: Path) -> None:
    raw, stages = replay.build(pilot_dir("8W20X"), "8W20X", "8W20X-CD1", tmp_path)
    derived = derive("8W20X", "CD1", raw, replay.recorded(stages), Catalog.load())
    assert derived["universal_hash_sha1"] == "1c0ecad91326f83551af3ae7e7ef8d7a5ea9c018"
    assert derived["write_offset_samples"] == 0
    assert derived["write_offset"]["method"] == "fallback-zero"
    assert derived["mcn"] == "0602537454587"
    assert derived["remaining_errors"]["q_program_sectors"] == 26
    tracks = derived["tracks"]
    assert len(tracks) == 14
    assert tracks[0]["isrc"] == "USQX91301302"
    assert tracks[0]["hashes"]["sha1"] == "acaa35a1edeab7e9b6be9839c7ab047a27e54eed"
    assert (tracks[13]["start_lba"], tracks[13]["length_sectors"]) == (272116, 6676)
    assert derived["sessions"][0]["lead_in"] == {
        "captured": True,
        "stored_range": [-150, 15],
        "verified": True,
        "attempts": 2,
    }
    checks = statuses(derived)
    for check in (
        "raw-sizes",
        "expected-files",
        "drive-identity",
        "toc-consistency",
        "track-layout",
        "track-errors",
        "track-extract",
        "plausibility-q-rate",
        "plausibility-dump-duration",
    ):
        assert checks[check] == "pass", check
    codes = {i["code"]: i["severity"] for i in derived["issues"]}
    assert codes == {"q-errors-remaining": "warning", "write-offset": "info"}

    schema = metadata.schema("medium")
    for key in DERIVED_KEYS:
        Draft202012Validator(schema["properties"][key]).validate(derived[key])


def test_jj314_split_refused(tmp_path: Path) -> None:
    raw, stages = replay.build(
        pilot_dir("JJ314"), "JJ314", "JJ314-CD1", tmp_path, split_exit=255
    )
    derived = derive("JJ314", "CD1", raw, replay.recorded(stages), Catalog.load())
    codes = {i["code"] for i in derived["issues"] if i["severity"] == "blocking"}
    assert {"split-refused", "c2-errors-remaining"} <= codes
    assert all(i["acceptable"] for i in derived["issues"])
    assert statuses(derived)["track-errors"] == "skip"
    assert derived["remaining_errors"]["c2_samples"] == 2077
    assert derived["tracks"] == []
    accepted = [
        {
            "code": "split-refused",
            "title": "t",
            "accepted_at": "2026-01-01T00:00:00Z",
            "reason": "r",
        }
    ]
    again = derive(
        "JJ314", "CD1", raw, replay.recorded(stages, accepted), Catalog.load()
    )
    assert next(i for i in again["issues"] if i["code"] == "split-refused")["accepted"]
