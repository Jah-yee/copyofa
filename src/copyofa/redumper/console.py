"""Parsers for redumper b753 console records and logs.

Every parser is tolerant of missing blocks and returns None (or an empty value) when its
block is absent.
"""

import re
from dataclasses import dataclass, field

from .rawdata import Counts

_HEADER_RE = re.compile(r"^\*\*\* (\S+) \(time check: \d+s\)$")
_SESSION_RE = re.compile(r"^\s*session (\d+)$")
_TRACK_RE = re.compile(r"^\s*track (\w+) \{\s*(audio|data)\s*\}$")
_DISC_INDEX_RE = re.compile(r"^\s*index (\d+) \{ LBA:\s*(-?\d+), MSF: [\d:]+ \}$")
_FINAL_INDEX_RE = re.compile(
    r"^\s*index (\d+) \{ LBA: \[\s*(-?\d+) \.\.\s*(-?\d+)\], length:\s*(\d+), "
    r"MSF: [\d:]+-[\d:]+ \}$"
)


def clean(raw: bytes) -> list[str]:
    """Decode a console record and drop carriage-return rewrites: keep the text after
    the last \\r.
    """
    text = raw.decode("utf-8", errors="replace")
    return [line.rsplit("\r", 1)[-1].rstrip() for line in text.split("\n")]


def sections(lines: list[str]) -> dict[str, list[str]]:
    """Split cleaned lines at `*** NAME (time check: Ns)` headers; lines before the
    first header are `HEADER`.

    A name that occurs more than once (several runs in one log) keeps its last
    occurrence.
    """
    result: dict[str, list[str]] = {}
    current = "HEADER"
    result[current] = []
    for line in lines:
        match = _HEADER_RE.match(line)
        if match:
            current = match.group(1)
            result[current] = []
        else:
            result[current].append(line)
    return result


def _block(lines: list[str], title: str) -> list[str] | None:
    """Lines following the line equal to `title` (after rstrip) up to the next
    empty line.
    """
    for i, line in enumerate(lines):
        if line == title:
            block: list[str] = []
            for follow in lines[i + 1 :]:
                if not follow:
                    break
                block.append(follow)
            return block
    return None


# --- header ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DriveInfo:
    path: str | None
    vendor: str
    product: str
    revision: str
    vendor_specific: str
    configuration_line: str | None
    drive_type: str | None
    read_offset: int | None
    c2_shift: int | None
    pregap_start: int | None
    read_method: str | None
    sector_order: str | None


@dataclass(frozen=True)
class Header:
    build: str | None
    arguments: str | None
    drive: DriveInfo | None
    profile: str | None
    read_speed: str | None


_INQUIRY_RE = re.compile(
    r"^  inquiry: (.*) - (.*) \(revision level: (.*), vendor specific: (.*)\)$"
)
_CONFIG_RE = re.compile(r"^  configuration: (\S+) \((.*)\)$")


def parse_header(lines: list[str]) -> Header | None:
    build = arguments = profile = read_speed = path = None
    inquiry = config_line = None
    for line in lines:
        if match := re.match(r"^redumper \(build: (\S+)\)$", line):
            if build is not None:
                break  # a second run starts: keep the first header only
            build = match.group(1)
        elif line.startswith("arguments: "):
            arguments = line[len("arguments: ") :]
        elif line.startswith("  path: "):
            path = line[len("  path: ") :]
        elif match := _INQUIRY_RE.match(line):
            inquiry = match
        elif line.startswith("  configuration: "):
            config_line = line.strip()
        elif line.startswith("  profile: "):
            profile = line[len("  profile: ") :]
        elif line.startswith("  read speed: "):
            read_speed = line[len("  read speed: ") :]
        elif _HEADER_RE.match(line):
            break
    if build is None:
        return None
    drive = None
    if inquiry:
        fields: dict[str, str] = {}
        drive_type = None
        if config_line and (match := _CONFIG_RE.match("  " + config_line)):
            drive_type = match.group(1)
            for part in match.group(2).split(", "):
                key, _, value = part.partition(": ")
                fields[key] = value

        def as_int(key: str) -> int | None:
            value = fields.get(key)
            return (
                int(value)
                if value is not None and re.fullmatch(r"[+-]?\d+", value)
                else None
            )

        drive = DriveInfo(
            path=path,
            vendor=inquiry.group(1),
            product=inquiry.group(2),
            revision=inquiry.group(3),
            vendor_specific=inquiry.group(4),
            configuration_line=config_line,
            drive_type=drive_type,
            read_offset=as_int("read offset"),
            c2_shift=as_int("C2 shift"),
            pregap_start=as_int("pre-gap start"),
            read_method=fields.get("read method"),
            sector_order=fields.get("sector order"),
        )
    return Header(
        build=build,
        arguments=arguments,
        drive=drive,
        profile=profile,
        read_speed=read_speed,
    )


# --- TOC ------------------------------------------------------------------------------


@dataclass(frozen=True)
class TocIndex:
    index: int
    lba: int
    lba_to: int | None = None
    length: int | None = None


@dataclass
class TocTrack:
    number: str  # "01".."99" or "AA" (lead-out)
    session: int
    type: str  # audio | data
    indices: list[TocIndex] = field(default_factory=list)

    def index(self, n: int) -> TocIndex | None:
        return next((i for i in self.indices if i.index == n), None)


@dataclass
class Toc:
    tracks: list[TocTrack]  # in order, including one AA entry per session

    @property
    def program_tracks(self) -> list[TocTrack]:
        return [t for t in self.tracks if t.number != "AA"]

    @property
    def sessions(self) -> list[int]:
        return sorted({t.session for t in self.tracks})

    def leadout(self, session: int | None = None) -> TocTrack | None:
        outs = [
            t
            for t in self.tracks
            if t.number == "AA" and (session is None or t.session == session)
        ]
        return outs[-1] if outs else None

    @property
    def leadout_lba(self) -> int | None:
        out = self.leadout()
        return out.indices[0].lba if out and out.indices else None


def _parse_toc_block(block: list[str] | None, final: bool) -> Toc | None:
    if block is None:
        return None
    tracks: list[TocTrack] = []
    session = 1
    index_re = _FINAL_INDEX_RE if final else _DISC_INDEX_RE
    for line in block:
        if match := _SESSION_RE.match(line):
            session = int(match.group(1))
        elif match := _TRACK_RE.match(line):
            tracks.append(TocTrack(match.group(1), session, match.group(2)))
        elif (match := index_re.match(line)) and tracks:
            if final:
                tracks[-1].indices.append(
                    TocIndex(
                        int(match.group(1)),
                        int(match.group(2)),
                        int(match.group(3)),
                        int(match.group(4)),
                    )
                )
            else:
                tracks[-1].indices.append(
                    TocIndex(int(match.group(1)), int(match.group(2)))
                )
    return Toc(tracks)


def parse_disc_toc(lines: list[str]) -> Toc | None:
    return _parse_toc_block(_block(lines, "disc TOC:"), final=False)


# --- counts ---------------------------------------------------------------------------


def _parse_counts(lines: list[str], title: str, q_suffix: str) -> Counts | None:
    block = _block(lines, title)
    if block is None:
        return None
    values: dict[str, int] = {}
    for line in block:
        if match := re.match(r"^  (SCSI|C2): (\d+) samples$", line):
            values[match.group(1)] = int(match.group(2))
        elif match := re.match(rf"^  Q: (\d+){q_suffix}$", line):
            values["Q"] = int(match.group(1))
    if set(values) != {"SCSI", "C2", "Q"}:
        return None
    return Counts(
        scsi_samples=values["SCSI"], c2_samples=values["C2"], q_sectors=values["Q"]
    )


def parse_media_errors(lines: list[str]) -> Counts | None:
    return _parse_counts(lines, "media errors:", "")


def parse_correction(lines: list[str]) -> Counts | None:
    return _parse_counts(lines, "correction statistics:", " sectors")


# --- lead-in --------------------------------------------------------------------------


@dataclass(frozen=True)
class LeadIn:
    attempts: int
    found: list[tuple[int, int, int]]
    not_identified: int
    discarded: list[tuple[int, int]]
    stored: tuple[int, int, bool] | None


def parse_leadin(lines: list[str]) -> LeadIn | None:
    attempts = not_identified = 0
    found: list[tuple[int, int, int]] = []
    discarded: list[tuple[int, int]] = []
    stored = None
    seen = False
    for line in lines:
        if not line.startswith("PLEXTOR: "):
            continue
        seen = True
        if re.match(r"^PLEXTOR: reading lead-in \(retry: \d+\)$", line):
            attempts += 1
        elif match := re.match(
            r"^PLEXTOR: lead-in found \(LBA: \[(-?\d+) \.\. (-?\d+)\], sectors: "
            r"(\d+)\)$",
            line,
        ):
            found.append(
                (int(match.group(1)), int(match.group(2)), int(match.group(3)))
            )
        elif line == "PLEXTOR: lead-in not identified":
            not_identified += 1
        elif match := re.match(
            r"^PLEXTOR: lead-in discarded as unverified \(LBA: \[(-?\d+) \.\. "
            r"(-?\d+)\]\)$",
            line,
        ):
            discarded.append((int(match.group(1)), int(match.group(2))))
        elif match := re.match(
            r"^PLEXTOR: storing lead-in \(LBA: \[(-?\d+) \.\. (-?\d+)\], verified: "
            r"(yes|no)\)$",
            line,
        ):
            stored = (int(match.group(1)), int(match.group(2)), match.group(3) == "yes")
    if not seen:
        return None
    return LeadIn(attempts, found, not_identified, discarded, stored)


def parse_protection(lines: list[str]) -> str | None:
    values = [
        line[len("protection: ") :] for line in lines if line.startswith("protection: ")
    ]
    return "; ".join(values) if values else None


def parse_drive_test(lines: list[str]) -> str | None:
    for line in lines:
        if match := re.match(r"^PLEXTOR lead-in: (yes|no|skipped)$", line.strip()):
            return match.group(1)
    return None


# --- split ----------------------------------------------------------------------------

OFFSET_METHOD_PATTERNS = [
    ("perfect-audio-offset", re.compile(r"^Perfect Audio Offset applied$")),
    (
        "moved-out-of-range",
        re.compile(
            r"^moving data out of (lead-out|lead-in|TOC) \(difference: [+-]\d+\)$"
        ),
    ),
    ("fallback-zero", re.compile(r"^fallback offset 0 applied$")),
    ("data-track", re.compile(r"^data disc detected, offset configuration:$")),
]


@dataclass
class Split:
    final_toc: Toc | None
    qtoc: Toc | None
    nonzero_toc_range: tuple[int, int] | None
    nonzero_data_range: tuple[int, int] | None
    universal_hash: str | None
    pao: tuple[int, list[tuple[int, int]]] | None
    pao_applied: bool
    offset_method: str | None
    offset_method_line: str | None
    write_offset: int | None
    track_errors: dict[str, tuple[int, int, int, int]]
    multisession: list[tuple[int, int, int]]
    cue_text: str | None
    refused: bool

    @property
    def qtoc_present(self) -> bool:
        return self.qtoc is not None


def _range(line: str, prefix: str) -> tuple[int, int] | None:
    match = re.match(
        rf"^{re.escape(prefix)} \[\s*([+-]?\d+) \.\.\s*([+-]?\d+)\]$", line
    )
    return (int(match.group(1)), int(match.group(2))) if match else None


def parse_split(lines: list[str]) -> Split | None:
    if not lines:
        return None
    final_toc = _parse_toc_block(_block(lines, "final TOC:"), final=True)
    qtoc = _parse_toc_block(_block(lines, "final QTOC:"), final=True)
    toc_range = data_range = None
    universal = None
    pao = None
    pao_applied = False
    method = method_line = None
    write_offset = None
    track_errors: dict[str, tuple[int, int, int, int]] = {}
    refused = False
    for line in lines:
        if (r := _range(line, "non-zero  TOC sample range:")) is not None:
            toc_range = r
        elif (r := _range(line, "non-zero data sample range:")) is not None:
            data_range = r
        elif match := re.match(r"^Universal Hash \(SHA-1\): ([0-9a-f]{40})$", line):
            universal = match.group(1)
        elif match := re.match(
            r"^Perfect Audio Offset \(silence level: (\d+)\): (.*)$", line
        ):
            ranges = []
            for item in re.finditer(
                r"\[([+-]?\d+) \.\. ([+-]?\d+)\]|([+-]?\d+)", match.group(2)
            ):
                if item.group(3) is not None:
                    ranges.append((int(item.group(3)), int(item.group(3))))
                else:
                    ranges.append((int(item.group(1)), int(item.group(2))))
            pao = (int(match.group(1)), ranges)
        elif match := re.match(r"^disc write offset: ([+-]?\d+)$", line):
            write_offset = int(match.group(1))
        elif match := re.match(
            r"^errors detected, track: (\S+), sectors: \{SKIP: (\d+), C2: (\d+)\}, "
            r"samples: \{SKIP: (\d+), C2: (\d+)\}$",
            line,
        ):
            track_errors[match.group(1)] = (
                int(match.group(2)),
                int(match.group(3)),
                int(match.group(4)),
                int(match.group(5)),
            )
        elif line == "error: data errors detected, unable to continue":
            refused = True
        if line == "Perfect Audio Offset applied":
            pao_applied = True
        for name, pattern in OFFSET_METHOD_PATTERNS:
            if pattern.match(line):
                method, method_line = name, line
    multisession = [
        (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        for line in _block(lines, "multisession:") or []
        if (match := re.match(r"^  session (\d+): (-?\d+)-(-?\d+)$", line))
    ]
    cue_text = None
    for i, line in enumerate(lines):
        if re.match(r"^CUE \[.*\]:$", line):
            cue_lines = []
            for follow in lines[i + 1 :]:
                if not follow:
                    break
                cue_lines.append(follow)
            cue_text = "\n".join(cue_lines) + "\n"
            break
    return Split(
        final_toc=final_toc,
        qtoc=qtoc,
        nonzero_toc_range=toc_range,
        nonzero_data_range=data_range,
        universal_hash=universal,
        pao=pao,
        pao_applied=pao_applied,
        offset_method=method,
        offset_method_line=method_line,
        write_offset=write_offset,
        track_errors=track_errors,
        multisession=multisession,
        cue_text=cue_text,
        refused=refused,
    )


# --- CUE ------------------------------------------------------------------------------

CD_TEXT_KEYWORDS = {
    "TITLE",
    "PERFORMER",
    "SONGWRITER",
    "COMPOSER",
    "ARRANGER",
    "MESSAGE",
    "DISC_ID",
    "GENRE",
    "TOC_INFO1",
    "TOC_INFO2",
    "UPC_EAN",
    "SIZE_INFO",
}


@dataclass
class CueFile:
    name: str
    track: int
    cue_type: str
    session: int
    isrc: str | None = None
    flags: set[str] = field(default_factory=set)
    cd_text: list[tuple[str, str]] = field(default_factory=list)
    indices: dict[int, int] = field(
        default_factory=dict
    )  # index -> frames from file start


@dataclass
class Cue:
    catalog: str | None
    cd_text: list[tuple[str, str]]
    rem: list[str]
    files: list[CueFile]
    session_rem: dict[
        int, dict[str, str]
    ]  # session -> {"LEAD-IN": "01:00:00", "PREGAP": ..., "LEAD-OUT": ...}


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def msf_frames(msf: str) -> int:
    minutes, seconds, frames = (int(x) for x in msf.split(":"))
    return (minutes * 60 + seconds) * 75 + frames


def parse_cue(text: str) -> Cue:
    catalog = None
    disc_text: list[tuple[str, str]] = []
    rem: list[str] = []
    files: list[CueFile] = []
    session_rem: dict[int, dict[str, str]] = {}
    session = 1
    pending_name: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        keyword, _, rest = line.partition(" ")
        if keyword == "REM":
            rem.append(line)
            rem_key, _, rem_value = rest.partition(" ")
            if rem_key == "SESSION":
                session = int(rem_value)
            elif rem_key in ("LEAD-IN", "LEAD-OUT", "PREGAP"):
                session_rem.setdefault(session, {})[rem_key] = rem_value.strip()
        elif keyword == "CATALOG":
            catalog = rest.strip()
        elif keyword == "FILE":
            match = re.match(r'^"(.*)" \S+$', rest)
            pending_name = match.group(1) if match else rest
        elif keyword == "TRACK":
            number, _, cue_type = rest.partition(" ")
            files.append(
                CueFile(
                    name=pending_name or "",
                    track=int(number),
                    cue_type=cue_type,
                    session=session,
                )
            )
        elif not files:
            disc_text.append((keyword, _unquote(rest)))
        elif keyword == "INDEX":
            number, _, msf = rest.partition(" ")
            files[-1].indices[int(number)] = msf_frames(msf.strip())
        elif keyword == "ISRC":
            files[-1].isrc = rest.strip()
        elif keyword == "FLAGS":
            files[-1].flags = set(rest.split())
        elif keyword in CD_TEXT_KEYWORDS:
            files[-1].cd_text.append((keyword, _unquote(rest)))
    return Cue(
        catalog=catalog,
        cd_text=disc_text,
        rem=rem,
        files=files,
        session_rem=session_rem,
    )


# --- hash -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Rom:
    size: int
    crc32: str
    md5: str
    sha1: str


_ROM_RE = re.compile(
    r'^<rom name="(.*)" size="(\d+)" crc="([0-9a-f]{8})" md5="([0-9a-f]{32})"'
    r' sha1="([0-9a-f]{40})" />$'
)


def parse_hash(lines: list[str]) -> dict[str, Rom]:
    roms: dict[str, Rom] = {}
    for line in lines:
        if match := _ROM_RE.match(line):
            roms[match.group(1)] = Rom(
                int(match.group(2)), match.group(3), match.group(4), match.group(5)
            )
    return roms
