import pytest

from copyofa.archive.analysis import track_check_ranges
from copyofa.errors import IntegrityFailure
from copyofa.redumper.console import Toc, TocIndex, TocTrack


def test_track_ends_at_next_entry() -> None:
    toc = Toc(
        [
            TocTrack("01", 1, "audio", [TocIndex(1, 0)]),
            TocTrack("AA", 1, "audio", [TocIndex(1, 1000)]),
        ]
    )
    assert track_check_ranges(toc) == [(1, 1, 0, 1000)]


def test_truncated_disc_toc_is_an_integrity_failure() -> None:
    # A disc TOC records no index ends, so without its lead-out entry the last
    # track has no end; this used to surface as an internal TypeError.
    toc = Toc([TocTrack("01", 1, "audio", [TocIndex(1, 0)])])
    with pytest.raises(IntegrityFailure, match="TOC track 01 has no end"):
        track_check_ranges(toc)
