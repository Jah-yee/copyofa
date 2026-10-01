"""Raw-data analysis of redumper .scram/.state/.subcode files.

Layout (redumper b753, CD): index 0 of .scram/.state/.subcode is LBA -45150; 588 stereo
samples per sector; .state holds one status byte per sample; .subcode holds 96 bytes per
sector (P-W interleaved, Q = bit 6).
"""

import binascii
import hashlib
import mmap
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

LBA_START = -45150
SAMPLES = 588
SECTOR = 2352
SUBCODE = 96

STATE_ERROR_SKIP = 0
STATE_ERROR_C2 = 1

_Q_BITS = bytes(
    (0x31 if b & 0x40 else 0x30) for b in range(256)
)  # byte -> ASCII '1'/'0' of its Q bit
_ISRC_TABLE = "0123456789_______ABCDEFGHIJKLMNOPQRSTUVWXYZ_____________________"


@dataclass(frozen=True)
class Counts:
    """Error counts. Console-parsed counts leave the program/lead-out split as None."""

    scsi_samples: int
    c2_samples: int
    q_sectors: int
    q_program_sectors: int | None = None
    q_leadout_sectors: int | None = None


def _read(path: Path) -> bytes:
    return path.read_bytes()


class _Mapped:
    """Read-only mmap of a file (empty files map to b"")."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = path.open("rb")
        size = self._file.seek(0, 2)
        self.data = (
            mmap.mmap(self._file.fileno(), 0, access=mmap.ACCESS_READ) if size else b""
        )

    def __enter__(self) -> mmap.mmap | bytes:
        return self.data

    def __exit__(self, *exc: object) -> None:
        if isinstance(self.data, mmap.mmap):
            self.data.close()
        self._file.close()


# --- Q subchannel ---------------------------------------------------------------------


def q_frames(subcode: bytes) -> list[bytes]:
    """De-interleave the 12-byte Q frame of every sector."""
    bits = subcode.translate(_Q_BITS)
    count = len(subcode) // SUBCODE
    return [
        int(bits[i * SUBCODE : (i + 1) * SUBCODE], 2).to_bytes(12, "big")
        for i in range(count)
    ]


def q_valid(q: bytes) -> bool:
    """CRC-16/GSM (poly 0x1021, init 0, xorout 0xFFFF) over Q[0:10] equals the
    big-endian Q[10:12].
    """
    return (binascii.crc_hqx(q[:10], 0) ^ 0xFFFF) == int.from_bytes(q[10:12], "big")


def _bcd_ok(byte: int) -> bool:
    return (byte >> 4) <= 9 and (byte & 0x0F) <= 9


def decode_mcn(q: bytes) -> str:
    """ADR 2: 7 BCD bytes -> 14 digits, last dropped (redumper TOC::updateMCN)."""
    return "".join(f"{(b >> 4) * 10 + (b & 0x0F):02}" for b in q[1:8])[:-1]


def mcn_plausible(q: bytes) -> bool:
    """Mode 2 layout: 13 BCD digits, 12 zero bits, AFRAME. A damaged mode-1 frame whose
    ADR nibble reads 2 is all BCD too, so the zero bits must also hold."""
    digits = []
    for b in q[1:8]:
        digits += [b >> 4, b & 0x0F]
    return all(d <= 9 for d in digits[:13]) and digits[13] == 0 and q[8] == 0


def decode_isrc(q: bytes) -> str:
    """ADR 3: five 6-bit characters, 2 padding bits, then bytes 4..7 as BCD -> 8 digits,
    last dropped."""
    isrc = q[1:9]
    bits = int.from_bytes(isrc[:4], "big")
    letters = "".join(
        _ISRC_TABLE[(bits >> (32 - 6 * (i + 1))) & 0x3F] for i in range(5)
    )
    digits = "".join(f"{(b >> 4) * 10 + (b & 0x0F):02}" for b in isrc[4:8])[:-1]
    return letters + digits


def isrc_plausible(q: bytes) -> bool:
    """Mode 3 layout: 5 x 6-bit characters (country code letters, then 3 alphanumerics),
    2 zero bits, 7 BCD digits, 4 zero bits, AFRAME. Damaged mode-1 frames whose ADR
    nibble reads 3 fail these rules."""
    value = decode_isrc(q)
    letters_ok = value[:2].isalpha() and "_" not in value[2:5]
    padding_ok = q[4] & 0x03 == 0 and q[8] & 0x0F == 0
    return letters_ok and padding_ok and all(_bcd_ok(b) for b in q[5:9])


@dataclass
class QAnalysis:
    runs: list[tuple[int, int]]  # bad-Q LBA runs (inclusive) at LBA >= pre-gap start
    total: int
    program: int
    leadout: int
    mcn_valid: Counter[str] = field(default_factory=Counter)
    isrc_valid: dict[int, Counter[str]] = field(default_factory=dict)
    mcn_first: str | None = None
    isrc_first: dict[int, str] = field(default_factory=dict)
    mcn_in_invalid_only: bool = False
    isrc_in_invalid_only: set[int] = field(default_factory=set)

    @property
    def longest_run(self) -> int:
        return max((b - a + 1 for a, b in self.runs), default=0)


def q_analysis(
    subcode: bytes,
    pregap_start: int,
    leadout_lba: int,
    track_ranges: list[tuple[int, int, int]],
) -> QAnalysis:
    """Analyse Q from the drive pre-gap start on.

    track_ranges: (track number, first LBA, LBA after the last) for MCN/ISRC
    attribution.
    """
    frames = q_frames(subcode)
    first_index = max(0, pregap_start - LBA_START)
    runs: list[tuple[int, int]] = []
    program = leadout = 0
    mcn_valid: Counter[str] = Counter()
    isrc_valid: dict[int, Counter[str]] = {}
    mcn_first = None
    isrc_first: dict[int, str] = {}
    mcn_invalid_seen = False
    isrc_invalid_seen: set[int] = set()
    ordered = sorted(track_ranges, key=lambda r: r[1])

    def track_at(lba: int) -> int | None:
        for number, start, end in ordered:
            if start <= lba < end:
                return number
        return None

    for index in range(first_index, len(frames)):
        q = frames[index]
        lba = index + LBA_START
        valid = q_valid(q)
        adr = q[0] & 0x0F
        if not valid:
            if lba < leadout_lba:
                program += 1
            else:
                leadout += 1
            if runs and runs[-1][1] == lba - 1:
                runs[-1] = (runs[-1][0], lba)
            else:
                runs.append((lba, lba))
            if adr == 2 and mcn_plausible(q):
                mcn_invalid_seen = True
            elif adr == 3 and isrc_plausible(q):
                track = track_at(lba)
                if track is not None:
                    isrc_invalid_seen.add(track)
            continue
        if adr == 2:
            value = decode_mcn(q)
            mcn_valid[value] += 1
            mcn_first = mcn_first or value
        elif adr == 3:
            track = track_at(lba)
            if track is not None:
                value = decode_isrc(q)
                isrc_valid.setdefault(track, Counter())[value] += 1
                isrc_first.setdefault(track, value)
    return QAnalysis(
        runs=runs,
        total=program + leadout,
        program=program,
        leadout=leadout,
        mcn_valid=mcn_valid,
        isrc_valid=isrc_valid,
        mcn_first=mcn_first,
        isrc_first=isrc_first,
        mcn_in_invalid_only=mcn_invalid_seen and not mcn_valid,
        isrc_in_invalid_only={t for t in isrc_invalid_seen if t not in isrc_valid},
    )


# --- read state -----------------------------------------------------------------------


def _first_sample(pregap_start: int, read_offset: int) -> int:
    return max(0, (pregap_start - LBA_START) * SAMPLES - read_offset)


def count_errors(
    state: Path, subcode: Path, pregap_start: int, read_offset: int, leadout_lba: int
) -> Counts:
    """Counts with redumper's semantics: samples and Q sectors from the drive pre-gap
    start on."""
    start = _first_sample(pregap_start, read_offset)
    with _Mapped(state) as data:
        region = data[start:]
        skip = region.count(bytes([STATE_ERROR_SKIP]))
        c2 = region.count(bytes([STATE_ERROR_C2]))
    q_program = q_leadout = 0
    frames = q_frames(_read(subcode))
    for index in range(max(0, pregap_start - LBA_START), len(frames)):
        if not q_valid(frames[index]):
            if index + LBA_START < leadout_lba:
                q_program += 1
            else:
                q_leadout += 1
    return Counts(skip, c2, q_program + q_leadout, q_program, q_leadout)


def error_runs(
    state: Path, status: int, pregap_start: int, read_offset: int
) -> list[tuple[int, int]]:
    """LBA runs (inclusive) of sectors holding at least one sample with the given
    status."""
    start = _first_sample(pregap_start, read_offset)
    needle = bytes([status])
    runs: list[tuple[int, int]] = []
    with _Mapped(state) as data:
        pos = data.find(needle, start) if data else -1
        while pos != -1:
            lba = (pos + read_offset) // SAMPLES + LBA_START
            if runs and runs[-1][1] >= lba - 1:
                runs[-1] = (runs[-1][0], max(runs[-1][1], lba))
            else:
                runs.append((lba, lba))
            pos = data.find(needle, (lba - LBA_START + 1) * SAMPLES - read_offset)
    return runs


def track_errors(
    state: Path, start_lba: int, length: int, write_offset: int
) -> tuple[int, int, int, int]:
    """(skip sectors, C2 sectors, skip samples, C2 samples) over a track file's
    samples, as redumper split counts."""
    base = (start_lba - LBA_START) * SAMPLES + write_offset
    result = []
    with _Mapped(state) as data:
        lo = max(0, base)
        region = data[lo : base + length * SAMPLES]
        shift = lo - base
        for status in (STATE_ERROR_SKIP, STATE_ERROR_C2):
            needle = bytes([status])
            samples = region.count(needle)
            sectors = 0
            pos = region.find(needle)
            while pos != -1:
                sectors += 1
                sector = (pos + shift) // SAMPLES
                pos = region.find(needle, (sector + 1) * SAMPLES - shift)
            result.append((sectors, samples))
    (skip_sectors, skip_samples), (c2_sectors, c2_samples) = result
    return skip_sectors, c2_sectors, skip_samples, c2_samples


def track_sha1(
    scram: Path, start_lba: int, length: int, write_offset: int
) -> str | None:
    """SHA-1 of the track file bytes cut from .scram; None if outside the file."""
    offset = ((start_lba - LBA_START) * SAMPLES + write_offset) * 4
    size = length * SECTOR
    if offset < 0 or offset + size > scram.stat().st_size:
        return None
    digest = hashlib.sha1()
    with scram.open("rb") as handle:
        handle.seek(offset)
        remaining = size
        while remaining:
            chunk = handle.read(min(remaining, 1 << 20))
            if not chunk:
                return None
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()
