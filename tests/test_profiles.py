import dataclasses
from typing import Any

from conftest import FIXTURES
from copyofa.acquisition import profiles
from copyofa.issues import ACCEPTED, FAIL, PASS, Issue, outcome
from copyofa.system import macos
from copyofa.system.macos import Medium


def medium(**changes: Any) -> Medium:
    xml = (FIXTURES / "drutil" / "8W20X-status.xml").read_bytes()
    status = macos.parse_drutil_status(xml, "CD-R   PX-W5224A")
    assert status is not None
    base = status.descriptor()
    return dataclasses.replace(base, **changes)


def test_pressed_audio_cd_matches_draft_profile() -> None:
    profile = profiles.match_profile(medium(), "CD")
    assert profile is not None
    assert profile.key == "cd-audio-pressed@0.1"
    assert not profile.approved([])
    assert profile.approved(["cd-audio-pressed@0.1"])
    assert [s.label for s in profile.stages if s.command == "refine"] == [
        "c2",
        "q",
        "slow",
    ]


def test_other_cds_are_provisional_only() -> None:
    cdr = profiles.match_profile(medium(media_type="CD-R", recordable=True), "CD")
    sacd = profiles.match_profile(medium(), "SACD")
    multi = profiles.match_profile(medium(sessions=2), "CD")
    for profile in (cdr, sacd, multi):
        assert profile is not None
        assert profile.name == "cd-provisional"
        assert not profile.approved(["cd-provisional@0.1"])


def test_no_profile_for_dvd() -> None:
    assert (
        profiles.match_profile(medium(family="DVD", media_type="DVD-ROM"), "DVD")
        is None
    )


def test_outcome() -> None:
    blocking = Issue("c2-errors-remaining", "blocking", "t", "w", "y")
    warning = Issue("q-errors-remaining", "warning", "t", "w", "y")
    assert outcome([warning], []) == PASS
    assert outcome([blocking, warning], []) == FAIL
    accepted = [
        {"code": "c2-errors-remaining", "title": "t", "accepted_at": "x", "reason": "r"}
    ]
    assert outcome([blocking], accepted) == ACCEPTED
    blocking.acceptable = False
    assert outcome([blocking], accepted) == FAIL
