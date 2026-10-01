from typing import Any

from conftest import FIXTURES
from copyofa.redumper import console
from copyofa.redumper.rawdata import Counts


def load(name: str) -> dict[str, list[str]]:
    return console.sections(
        console.clean((FIXTURES / "logs" / f"{name}.log").read_bytes())
    )


def summary(sections: dict[str, list[str]]) -> dict[str, Any]:
    split = console.parse_split(sections["SPLIT"])
    assert split is not None
    assert split.cue_text is not None
    cue = console.parse_cue(split.cue_text)
    roms = console.parse_hash(sections["HASH"])
    toc = console.parse_disc_toc(sections["DUMP"])
    assert toc is not None
    return {
        "header": console.parse_header(sections["HEADER"]),
        "media": console.parse_media_errors(sections["DUMP"]),
        "correction": console.parse_correction(sections["REFINE"]),
        "leadin": console.parse_leadin(sections["DUMP::EXTRA"]),
        "protection": console.parse_protection(sections["PROTECTION"]),
        "toc": [(t.number, [(i.index, i.lba) for i in t.indices]) for t in toc.tracks],
        "split": split,
        "cue": cue,
        "roms": roms,
    }


def test_8w20x() -> None:
    s = summary(load("8W20X"))
    drive = s["header"].drive
    assert s["header"].build == "b753+d8fix"
    assert (drive.vendor, drive.product, drive.revision) == (
        "PLEXTOR",
        "CD-R PX-W5224A",
        "1.04",
    )
    assert (
        drive.read_offset,
        drive.c2_shift,
        drive.pregap_start,
        drive.read_method,
    ) == (30, 294, -75, "D8")
    assert s["media"] == Counts(0, 0, 129)
    assert s["correction"] == Counts(0, 0, 103)
    assert s["leadin"].stored == (-150, 15, True)
    assert s["leadin"].attempts == 2
    assert s["protection"] == "none"
    assert len(s["toc"]) == 15
    assert s["toc"][-1] == ("AA", [(1, 278792)])
    split = s["split"]
    assert split.universal_hash == "1c0ecad91326f83551af3ae7e7ef8d7a5ea9c018"
    assert split.pao == (0, [(-88200, 634)])
    assert split.offset_method_line == "fallback offset 0 applied"
    assert split.write_offset == 0
    assert not split.refused
    assert not split.qtoc_present
    assert split.track_errors == {}
    assert split.final_toc.leadout_lba == 278792
    assert split.final_toc.program_tracks[0].index(0).length == 150
    cue = s["cue"]
    assert cue.catalog == "0602537454587"
    assert ("TITLE", "Hesitation Marks") in cue.cd_text
    assert cue.files[0].isrc == "USQX91301302"
    assert cue.files[0].indices == {1: 0}
    assert (
        s["roms"]["8W20X (Track 01).bin"].sha1
        == "acaa35a1edeab7e9b6be9839c7ab047a27e54eed"
    )


def test_jj314_offset_and_refusal() -> None:
    split = console.parse_split(load("JJ314")["SPLIT"])
    assert split is not None
    assert split.write_offset == -296
    assert split.offset_method_line == "moving data out of lead-in (difference: -296)"
    assert split.track_errors == {"13": (0, 66, 0, 869), "14": (0, 112, 0, 1208)}
    assert split.refused
    assert split.universal_hash is None


def test_mixedmode_sessions() -> None:
    sections = load("MIXEDMODE")
    toc = console.parse_disc_toc(sections["DUMP"])
    assert toc is not None
    assert toc.sessions == [1, 2]
    leadout = toc.leadout(1)
    assert leadout is not None
    assert leadout.indices[0].lba == 220562
    assert toc.leadout_lba == 350210
    split = console.parse_split(sections["SPLIT"])
    assert split is not None
    assert split.multisession == [(1, 0, 220561), (2, 231962, 350209)]
    assert split.offset_method == "data-track"
    assert split.cue_text is not None
    cue = console.parse_cue(split.cue_text)
    assert cue.session_rem == {
        1: {"LEAD-OUT": "01:30:00"},
        2: {"LEAD-IN": "01:00:00", "PREGAP": "00:02:00"},
    }
    assert cue.files[-1].session == 2
    assert cue.files[-1].cue_type == "MODE1/2352"
    assert cue.files[1].indices == {0: 0, 1: 150}


def test_progress_rewrites_and_verbose_lines_do_not_change_results() -> None:
    raw = (FIXTURES / "logs" / "8W20X.log").read_bytes()
    noisy = raw.replace(
        b"media errors: \n",
        b"[LBA: 1234] C2 error (bits:   12)\n/ [ 10%] LBA: 12345/278792\r"
        + b" " * 79
        + b"\rmedia errors: \n",
    ).replace(
        b"protection: none\n", b"progress\r" + b" " * 79 + b"\rprotection: none\n"
    )
    assert noisy != raw
    assert summary(console.sections(console.clean(noisy))) == summary(
        console.sections(console.clean(raw))
    )


def test_drive_test_line() -> None:
    assert console.parse_drive_test(["  PLEXTOR lead-in: yes"]) == "yes"
    assert console.parse_drive_test(["nothing"]) is None
