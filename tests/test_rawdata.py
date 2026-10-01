import binascii

import pytest
from conftest import FIXTURES, pilot_file

from copyofa.redumper import console, rawdata


def q_to_subcode(q: bytes) -> bytes:
    """Interleave a 12-byte Q frame into 96 subcode bytes (Q = bit 6)."""
    bits = int.from_bytes(q, "big")
    return bytes(0x40 if bits >> (95 - i) & 1 else 0 for i in range(96))


def with_crc(body: bytes) -> bytes:
    return body + (binascii.crc_hqx(body, 0) ^ 0xFFFF).to_bytes(2, "big")


def bcd(n: int) -> int:
    return (n // 10) << 4 | n % 10


MCN_Q = with_crc(bytes([0x02]) + bytes(bcd(int("06025374545870"[i : i + 2])) for i in range(0, 14, 2)) + b"\x00\x10")


def isrc_q(isrc: str) -> bytes:
    table = rawdata._ISRC_TABLE
    bits = 0
    for char in isrc[:5]:
        bits = bits << 6 | table.index(char)
    letters = (bits << 2).to_bytes(4, "big")
    digits = isrc[5:] + "0"
    return with_crc(bytes([0x03]) + letters + bytes(bcd(int(digits[i : i + 2])) for i in range(0, 8, 2)) + b"\x10")


def test_q_crc_validity():
    good = MCN_Q
    broken = good[:11] + bytes([good[11] ^ 1])
    frames = rawdata.q_frames(q_to_subcode(good) + q_to_subcode(broken) + bytes(96))
    assert frames[0] == good
    assert [rawdata.q_valid(q) for q in frames] == [True, False, False]


def test_mcn_and_isrc_decoding():
    assert rawdata.decode_mcn(MCN_Q) == "0602537454587"
    assert rawdata.decode_isrc(isrc_q("USQX91301302")) == "USQX91301302"


def test_damaged_mode1_frames_are_not_mistaken_for_isrc_or_mcn():
    # JJ314 LBA 228863: a mode-1 frame (track 12, index 01) whose ADR nibble was damaged to 3, then to 2
    damaged = bytes.fromhex("03120100026100505338c844")
    assert not rawdata.isrc_plausible(damaged)
    assert not rawdata.mcn_plausible(bytes([0x02]) + damaged[1:])
    assert rawdata.isrc_plausible(isrc_q("USQX91301302")) and rawdata.mcn_plausible(MCN_Q)


def test_q_analysis_invalid_only_detection():
    broken_mcn = MCN_Q[:11] + bytes([MCN_Q[11] ^ 1])
    leadin = bytes(96) * (-rawdata.LBA_START - 75)  # LBA -45150 .. -76 are ignored
    good_isrc = q_to_subcode(isrc_q("USQX91301302"))
    sub = leadin + q_to_subcode(broken_mcn) + good_isrc + bytes(96)
    analysis = rawdata.q_analysis(sub, -75, -73, [(1, -75, -73)])
    assert analysis.mcn_in_invalid_only and not analysis.mcn_valid
    assert analysis.isrc_first == {1: "USQX91301302"} and not analysis.isrc_in_invalid_only
    assert (analysis.program, analysis.leadout, analysis.runs) == (1, 1, [(-75, -75), (-73, -73)])


PILOT_Q = {
    "8W20X": (26, 0, 278792),
    "REISE": (731, 1, 218136),
    "Broken-SteveVonSteve-T02PQ": (545, 2, 149042),
    "JJ314": (1460, 0, 287827),
    "KGWHD": (130, 0, 241800),
    "08T2J": (25, 0, 293072),
    "7PSQ4": (21, 0, 293072),
}


@pytest.mark.pilot
@pytest.mark.parametrize("name", PILOT_Q)
def test_pilot_remaining_q(name):
    program, leadout, leadout_lba = PILOT_Q[name]
    counts = rawdata.count_errors(pilot_file(name, "state"), pilot_file(name, "subcode"), -75, 30, leadout_lba)
    assert (counts.q_program_sectors, counts.q_leadout_sectors) == (program, leadout)
    assert counts.q_sectors == program + leadout


@pytest.mark.pilot
def test_pilot_jj314_errors():
    counts = rawdata.count_errors(pilot_file("JJ314", "state"), pilot_file("JJ314", "subcode"), -75, 30, 287827)
    assert counts.c2_samples == 2077 and counts.scsi_samples == 0
    sections = console.sections(console.clean((FIXTURES / "logs" / "JJ314.log").read_bytes()))
    split = console.parse_split(sections["SPLIT"])
    tracks = split.final_toc.tracks
    for track in (13, 14):
        # split refused, so no CUE: redumper checks each track from its first index to the next track's
        start = tracks[track - 1].indices[0].lba
        length = tracks[track].indices[0].lba - start
        errors = rawdata.track_errors(pilot_file("JJ314", "state"), start, length, -296)
        assert errors == split.track_errors[str(track)]

@pytest.mark.pilot
def test_pilot_8w20x_track_extract_and_identifiers():
    scram = pilot_file("8W20X", "scram")
    assert rawdata.track_sha1(scram, 0, 3934, 0) == "acaa35a1edeab7e9b6be9839c7ab047a27e54eed"
    assert rawdata.track_sha1(scram, 272116, 6676, 0) == "7b7e7f781dde53d0dd81c7e34cfbd84e5eafcb16"
    analysis = rawdata.q_analysis(pilot_file("8W20X", "subcode").read_bytes(), -75, 278792, [(1, 0, 3934)])
    assert analysis.mcn_first == "0602537454587" and len(analysis.mcn_valid) == 1
    assert analysis.isrc_first[1] == "USQX91301302"
    assert analysis.program == 26 and analysis.longest_run >= 1
