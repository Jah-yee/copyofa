"""Re-derivable metadata, consistency checks and issues.

`derive()` is the single function used at archive time, by `coa reprocess` and by
deep verify: it reads only the raw files (redumper output and session records) and
the recorded fields, never the work state.
"""

import re
from pathlib import Path
from typing import Any

from ..errors import IntegrityFailure
from ..issues import Issue
from ..redumper import console, rawdata
from ..redumper.messages import Catalog
from ..system import macos
from ..util import parse_utc, sha256_file
from . import encoding

REDUMPER_OUTPUTS = (
    "scram",
    "state",
    "subcode",
    "toc",
    "fulltoc",
    "cdtext",
    "atip",
    "pma",
    "log",
)
REQUIRED_OUTPUTS = ("scram", "state", "subcode", "toc", "fulltoc", "log")
MAX_RUNS = 1000


def _flag(ok: bool) -> str:
    return "pass" if ok else "fail"


def _check(id_: str, status: str, values: dict[str, Any], note: str) -> dict[str, Any]:
    return {
        "id": id_,
        "origin": "derived",
        "status": status,
        "values": values,
        "note": note,
    }


class Records:
    """Cleaned lines of the session records, cached."""

    def __init__(self, raw: dict[str, Path]) -> None:
        self.raw = raw
        self._cache: dict[str, list[str]] = {}

    def lines(self, name: str | None) -> list[str]:
        if name is None or name not in self.raw:
            return []
        if name not in self._cache:
            self._cache[name] = console.clean(self.raw[name].read_bytes())
        return self._cache[name]

    def section(self, name: str | None, command: str) -> list[str]:
        return console.sections(self.lines(name)).get(command.upper(), [])


def _ioreg_identity(text: str) -> dict[str, Any]:
    def find(key: str) -> str | None:
        match = re.search(rf'"{re.escape(key)}" = "?([^"\n]*)"?', text)
        return match.group(1).strip() if match else None

    vid, pid = find("idVendor"), find("idProduct")
    bridge = None
    if vid and pid and vid.isdigit() and pid.isdigit():
        bridge = {
            "vendor_id": f"0x{int(vid):04X}",
            "product_id": f"0x{int(pid):04X}",
            "name": find("USB Product Name"),
        }
    return {
        "vendor": find("Vendor Identification"),
        "product": find("Product Identification"),
        "revision": find("Product Revision Level"),
        "bridge": bridge,
    }


def _interconnect(xml: bytes) -> str | None:
    import xml.etree.ElementTree as ET

    node = ET.fromstring(xml).find("statusfordevice/device/interconnect")
    return node.get("name") if node is not None else None


def _collapse(text: str | None) -> str:
    return " ".join((text or "").split())


def _stage(
    stages: list[dict[str, Any]], name: str, *, first: bool = False, ok: bool = True
) -> dict[str, Any] | None:
    candidates = [
        s
        for s in stages
        if s["nn"] is not None and s["name"] == name and (not ok or s["exit_code"] == 0)
    ]
    if not candidates and ok:
        return _stage(stages, name, first=first, ok=False)
    if not candidates:
        return None
    candidates.sort(key=lambda s: s["nn"])
    return candidates[0] if first else candidates[-1]


def _reported(value: int | None, what: str) -> int:
    """A drive value from the dump record's configuration line, which the raw-data
    analysis cannot do without."""
    if value is None:
        raise IntegrityFailure(
            f"the dump console record does not report the drive's {what}; "
            "the raw files cannot be analyzed"
        )
    return value


def track_check_ranges(toc: console.Toc) -> list[tuple[int, int, int, int]]:
    """(track number, session, first LBA, LBA after the last) as redumper's
    check_tracks uses them.
    """
    ranges = []
    tracks = toc.tracks
    for i, track in enumerate(tracks):
        if track.number == "AA" or not track.indices:
            continue
        nxt = tracks[i + 1] if i + 1 < len(tracks) else None
        if nxt and nxt.indices:
            end = nxt.indices[0].lba
        else:
            # Only the final TOC records index ends; a disc TOC always closes with
            # the lead-out entry, so a missing end means the record is truncated.
            last_lba = track.indices[-1].lba_to
            if last_lba is None:
                raise IntegrityFailure(
                    f"TOC track {track.number} has no end: the TOC in the console "
                    "record is incomplete"
                )
            end = last_lba + 1
        ranges.append((int(track.number), track.session, track.indices[0].lba, end))
    return ranges


def derive(
    holon: str,
    medium: str,
    raw: dict[str, Path],
    recorded: dict[str, Any],
    catalog: Catalog,
) -> dict[str, Any]:
    image = f"{holon}-{medium}"
    rec = Records(raw)
    stages = recorded["stages"]
    accepted_codes = {a["code"] for a in recorded["accepted_issues"]}
    strict_q = bool((recorded.get("profile") or {}).get("strict_q"))
    checks: list[dict[str, Any]] = [
        c for c in recorded.get("checks", []) if c.get("origin") == "acquisition"
    ]
    issues: dict[str, Issue] = {}

    def add(issue: Issue) -> None:
        issues[issue.code] = issue

    refine_cmd = f"coa refine {holon} {medium} --retries 100 --speed 8"

    def accept(code: str) -> str:
        return f"coa accept {holon} {medium} {code} --reason TEXT"

    # --- sources ---
    dump = _stage(stages, "dump", first=True)
    extra = _stage(stages, "dump::extra")
    protection_stage = _stage(stages, "protection")
    split_stage = max(
        (s for s in stages if s["name"] == "split" and s["nn"] is not None),
        key=lambda s: s["nn"],
        default=None,
    )
    hash_stage = info_stage = None
    if split_stage:
        following = {
            s["nn"]: s
            for s in stages
            if s["nn"] is not None and s["nn"] > split_stage["nn"]
        }
        hash_stage = (
            following.get(split_stage["nn"] + 1)
            if following.get(split_stage["nn"] + 1, {}).get("name") == "hash"
            else None
        )
        info_stage = (
            following.get(split_stage["nn"] + 2)
            if following.get(split_stage["nn"] + 2, {}).get("name") == "info"
            else None
        )
    dump_record = dump["record"] if dump else None
    header = console.parse_header(rec.lines(dump_record))
    dinfo = header.drive if header else None
    disc_toc = console.parse_disc_toc(rec.section(dump_record, "dump"))
    split = (
        console.parse_split(rec.section(split_stage["record"], "split"))
        if split_stage
        else None
    )
    cue = console.parse_cue(split.cue_text) if split and split.cue_text else None
    roms = (
        console.parse_hash(rec.section(hash_stage["record"], "hash"))
        if hash_stage
        else {}
    )
    leadin = (
        console.parse_leadin(rec.section(extra["record"], "dump::extra"))
        if extra
        else None
    )
    protection = (
        console.parse_protection(rec.section(protection_stage["record"], "protection"))
        if protection_stage
        else None
    )
    drutil_name = f"{image}.preflight-drutil.txt"
    drutil_xml = raw[drutil_name].read_bytes() if drutil_name in raw else None
    status = macos.parse_drutil_status(drutil_xml, None) if drutil_xml else None
    descriptor = status.descriptor() if status else None
    ioreg_name = f"{image}.preflight-ioreg.txt"
    ioreg = (
        _ioreg_identity(raw[ioreg_name].read_text(errors="replace"))
        if ioreg_name in raw
        else {}
    )
    test_name = f"{image}.preflight-drive-test.console"
    plextor_leadin = (
        console.parse_drive_test(rec.lines(test_name)) if test_name in raw else None
    )
    toc = split.final_toc if split and split.final_toc else disc_toc
    pregap_start = dinfo.pregap_start if dinfo else -75
    read_offset = dinfo.read_offset if dinfo else 0
    leadout_lba = disc_toc.leadout_lba if disc_toc else None
    write_offset = split.write_offset if split and split.write_offset is not None else 0

    # --- drive and medium ---
    drive = {
        "vendor": dinfo.vendor if dinfo else None,
        "product": dinfo.product if dinfo else None,
        "firmware": dinfo.revision if dinfo else None,
        "vendor_specific": dinfo.vendor_specific if dinfo else None,
        "drive_type": dinfo.drive_type if dinfo else None,
        "read_offset": read_offset,
        "c2_shift": dinfo.c2_shift if dinfo else None,
        "pregap_start": pregap_start,
        "read_method": dinfo.read_method if dinfo else None,
        "sector_order": dinfo.sector_order if dinfo else None,
        "configuration_line": dinfo.configuration_line if dinfo else None,
        "connection": {
            "interconnect": _interconnect(drutil_xml) if drutil_xml else None,
            "bridge": ioreg.get("bridge"),
        },
        "drive_test": {
            "record": test_name if test_name in raw else None,
            "plextor_leadin": plextor_leadin,
        },
    }
    data_track = any(t.type == "data" for t in toc.program_tracks) if toc else False
    medium_doc = {
        "family": descriptor.family if descriptor else None,
        "media_type": descriptor.media_type if descriptor else None,
        "recordable": descriptor.recordable if descriptor else None,
        "rewritable": descriptor.rewritable if descriptor else None,
        "layers": 1,
        "disc_status": descriptor.disc_status if descriptor else None,
        "last_session_state": descriptor.last_session_state if descriptor else None,
        "sessions": descriptor.sessions if descriptor else None,
        "toc_source": "TOC",
        "protection": protection,
    }

    # --- raw files ---
    audio_scram = medium_doc["family"] == "CD" and not data_track
    raw_files = [
        {
            "name": name,
            "size": raw[name].stat().st_size,
            "sha256": sha256_file(raw[name]),
            "stored_as": encoding.stored_name(name, audio_scram),
        }
        for name in sorted(raw)
    ]
    outputs = {
        name[len(image) + 1 :]
        for name in raw
        if re.fullmatch(rf"{re.escape(image)}\.[a-z]+", name)
    }

    # --- error counts from the final raw files ---
    state_path, subcode_path = raw.get(f"{image}.state"), raw.get(f"{image}.subcode")
    counts = q = None
    ranges = track_check_ranges(toc) if toc else []
    if state_path and subcode_path and leadout_lba is not None:
        counts = rawdata.count_errors(
            state_path,
            subcode_path,
            _reported(pregap_start, "pre-gap start"),
            _reported(read_offset, "read offset"),
            leadout_lba,
        )
        q = rawdata.q_analysis(
            subcode_path.read_bytes(),
            _reported(pregap_start, "pre-gap start"),
            leadout_lba,
            [(n, a, b) for n, _, a, b in ranges],
        )
    remaining = {
        "scsi_samples": counts.scsi_samples if counts else None,
        "c2_samples": counts.c2_samples if counts else None,
        "q_program_sectors": counts.q_program_sectors if counts else None,
        "q_leadout_sectors": counts.q_leadout_sectors if counts else None,
        "q_runs": [list(r) for r in q.runs[:MAX_RUNS]] if q else [],
    }

    # --- tracks ---
    tracks: list[dict[str, Any]] = []
    if cue and split and split.final_toc:
        final_tracks = {int(t.number): t for t in split.final_toc.program_tracks}
        for entry in cue.files:
            t = final_tracks.get(entry.track)
            index1 = t.index(1) if t else None
            start = index1.lba - entry.indices.get(1, 0) if index1 else None
            rom = roms.get(entry.name)
            length = rom.size // rawdata.SECTOR if rom else None
            errors = None
            if state_path and start is not None and length is not None:
                skip_sectors, c2_sectors, skip_samples, c2_samples = (
                    rawdata.track_errors(state_path, start, length, write_offset)
                )
                errors = {
                    "skip_sectors": skip_sectors,
                    "c2_sectors": c2_sectors,
                    "skip_samples": skip_samples,
                    "c2_samples": c2_samples,
                }
            isrc = (q.isrc_first.get(entry.track) if q else None) or entry.isrc
            tracks.append(
                {
                    "number": entry.track,
                    "session": entry.session,
                    "type": t.type if t else None,
                    "cue_type": entry.cue_type,
                    "start_lba": start,
                    "length_sectors": length,
                    "indices": [
                        {"index": i.index, "lba": i.lba, "length": i.length}
                        for i in (t.indices if t else [])
                    ],
                    "flags": {
                        "pre_emphasis": "PRE" in entry.flags,
                        "digital_copy": "DCP" in entry.flags,
                        "four_channel": "4CH" in entry.flags,
                    },
                    "isrc": isrc,
                    "cd_text": [{"field": k, "value": v} for k, v in entry.cd_text],
                    "file_name": entry.name,
                    "hashes": {
                        "size": rom.size,
                        "crc32": rom.crc32,
                        "md5": rom.md5,
                        "sha1": rom.sha1,
                    }
                    if rom
                    else None,
                    "errors": errors,
                }
            )

    # --- sessions ---
    sessions: list[dict[str, Any]] = []
    if toc:
        subcode_sectors = (
            subcode_path.stat().st_size // rawdata.SUBCODE if subcode_path else None
        )
        for number in toc.sessions:
            program = [t for t in toc.program_tracks if t.session == number]
            out = toc.leadout(number)
            out_lba = out.indices[0].lba if out and out.indices else None
            first = program[0] if program else None
            last_session = number == toc.sessions[-1]
            stored = leadin.stored if (leadin and number == 1) else None
            sessions.append(
                {
                    "number": number,
                    "first_track": int(first.number) if first else None,
                    "last_track": int(program[-1].number) if program else None,
                    "lead_in": {
                        "captured": bool(stored),
                        "stored_range": [stored[0], stored[1]] if stored else None,
                        "verified": bool(stored and stored[2]),
                        "attempts": leadin.attempts if (leadin and number == 1) else 0,
                    },
                    "pregap_sectors": (index0.length or 0)
                    if first and (index0 := first.index(0))
                    else 0,
                    "leadout_lba": out_lba,
                    "leadout_sectors_read": (
                        subcode_sectors - (out_lba - rawdata.LBA_START)
                        if last_session
                        and subcode_sectors is not None
                        and out_lba is not None
                        else None
                    ),
                    "cue_rem": (cue.session_rem.get(number, {}) if cue else {}),
                }
            )

    # --- checks ---
    if state_path and subcode_path and f"{image}.scram" in raw:
        sub_size, state_size = subcode_path.stat().st_size, state_path.stat().st_size
        scram_size = raw[f"{image}.scram"].stat().st_size
        expected = (
            sub_size // rawdata.SUBCODE * rawdata.SECTOR
            - _reported(read_offset, "read offset") * 4
        )
        ok = (
            sub_size % rawdata.SUBCODE == 0
            and scram_size == 4 * state_size
            and scram_size == expected
        )
        checks.append(
            _check(
                "raw-sizes",
                _flag(ok),
                {
                    "scram": scram_size,
                    "state": state_size,
                    "subcode": sub_size,
                    "expected_scram": expected,
                    "read_offset": read_offset,
                },
                ".subcode % 96 == 0, .scram == 4 x .state == sectors x 2352 - read"
                " offset x 4",
            )
        )
    unexpected = sorted(outputs - set(REDUMPER_OUTPUTS))
    missing = sorted(set(REQUIRED_OUTPUTS) - outputs)
    cdr_files = sorted(outputs & {"atip", "pma"})
    bad_cdr = bool(cdr_files) and descriptor is not None and not descriptor.recordable
    checks.append(
        _check(
            "expected-files",
            _flag(not unexpected and not missing and not bad_cdr),
            {
                "present": sorted(outputs),
                "unexpected": unexpected,
                "missing": missing,
                "atip_pma": cdr_files,
            },
            "redumper outputs vs the expected CD set; .atip/.pma only on"
            " recordable media",
        )
    )
    if dinfo and ioreg.get("vendor"):
        ok = (_collapse(dinfo.vendor), _collapse(dinfo.product), dinfo.revision) == (
            _collapse(ioreg["vendor"]),
            _collapse(ioreg["product"]),
            ioreg["revision"],
        )
        checks.append(
            _check(
                "drive-identity",
                _flag(ok),
                {
                    "inquiry": [dinfo.vendor, dinfo.product, dinfo.revision],
                    "ioreg": [ioreg["vendor"], ioreg["product"], ioreg["revision"]],
                },
                "redumper inquiry vs the drive identification recorded at preflight",
            )
        )
    if disc_toc and split and split.final_toc and status:
        disc = [
            (int(t.number), idx.lba if (idx := t.index(1)) else None)
            for t in disc_toc.program_tracks
        ]
        final = [
            (int(t.number), idx.lba if (idx := t.index(1)) else None)
            for t in split.final_toc.program_tracks
        ]
        drutil_tracks = [(t.number, t.start) for t in status.tracks]
        last = status.tracks[-1] if status.tracks else None
        drutil_end = last.start + last.size if last else None
        final_leadout = split.final_toc.leadout_lba
        ok = (
            disc == final == drutil_tracks
            and disc_toc.leadout_lba == final_leadout
            and (len(split.final_toc.sessions) > 1 or drutil_end == final_leadout)
        )
        checks.append(
            _check(
                "toc-consistency",
                _flag(ok),
                {
                    "disc_toc": disc,
                    "final_toc": final,
                    "drutil": drutil_tracks,
                    "leadout": [disc_toc.leadout_lba, final_leadout, drutil_end],
                },
                "disc TOC, final TOC and drutil track starts and lead-out agree",
            )
        )
    if tracks:
        problems = []
        for i, row in enumerate(tracks):
            if row["hashes"] is None or row["hashes"]["size"] % rawdata.SECTOR:
                problems.append(
                    f"track {row['number']}: size not a multiple of 2352 or missing"
                )
                continue
            nxt = tracks[i + 1] if i + 1 < len(tracks) else None
            if nxt and nxt["session"] == row["session"]:
                if row["start_lba"] + row["length_sectors"] != nxt["start_lba"]:
                    problems.append(
                        f"track {row['number']} does not end where track"
                        f" {nxt['number']} starts"
                    )
            else:
                out = toc.leadout(row["session"]) if toc else None
                if (
                    out is None
                    or row["start_lba"] + row["length_sectors"] != out.indices[0].lba
                ):
                    problems.append(
                        f"track {row['number']} does not end at the session lead-out"
                    )
        checks.append(
            _check(
                "track-layout",
                _flag(not problems),
                {"problems": problems},
                "track files are whole sectors, contiguous and end at the session"
                " lead-out",
            )
        )
    if split and state_path and not tracks:
        checks.append(
            _check(
                "track-errors",
                "skip",
                {},
                "no track files (split refused): no canonical track ranges",
            )
        )
    elif split and tracks:
        mismatches = []
        # Track-file ranges: start_lba = final-TOC INDEX 01 - CUE INDEX 01,
        # length = ROM size / 2352.
        for row in tracks:
            if row["errors"] is None:
                mismatches.append(
                    {"track": row["number"], "computed": None, "reported": None}
                )
                continue
            errors = row["errors"]
            computed = (
                errors["skip_sectors"],
                errors["c2_sectors"],
                errors["skip_samples"],
                errors["c2_samples"],
            )
            reported = split.track_errors.get(f"{row['number']:02d}", (0, 0, 0, 0))
            if tuple(computed) != tuple(reported):
                mismatches.append(
                    {
                        "track": row["number"],
                        "computed": list(computed),
                        "reported": list(reported),
                    }
                )
        checks.append(
            _check(
                "track-errors",
                _flag(not mismatches),
                {"mismatches": mismatches},
                "per-track SKIP/C2 counts from .state vs redumper split's errors"
                " detected lines",
            )
        )
    if tracks and f"{image}.scram" in raw:
        mismatches = []
        for row in tracks:
            if row["type"] != "audio" or row["hashes"] is None:
                continue
            sha1 = rawdata.track_sha1(
                raw[f"{image}.scram"],
                row["start_lba"],
                row["length_sectors"],
                write_offset,
            )
            if sha1 != row["hashes"]["sha1"]:
                mismatches.append(
                    {
                        "track": row["number"],
                        "extracted": sha1,
                        "split": row["hashes"]["sha1"],
                    }
                )
        checks.append(
            _check(
                "track-extract",
                _flag(not mismatches),
                {"mismatches": mismatches},
                "audio tracks cut from .scram equal split's track hashes",
            )
        )
    first_pass = (recorded.get("acquisition_errors") or {}).get("first_pass") or {}
    rate = first_pass.get("q_program_rate")
    if rate is None:
        checks.append(
            _check(
                "plausibility-q-rate", "skip", {"rate": None}, "no first-pass Q rate"
            )
        )
    else:
        checks.append(
            _check(
                "plausibility-q-rate",
                "pass" if 0.0001 <= rate <= 0.015 else "warn",
                {"rate": rate, "range": [0.0001, 0.015]},
                "first-pass program-area bad-Q rate",
            )
        )
    if dump and dump.get("started_at") and dump.get("ended_at"):
        duration = (
            parse_utc(dump["ended_at"]) - parse_utc(dump["started_at"])
        ).total_seconds()
        checks.append(
            _check(
                "plausibility-dump-duration",
                "pass" if 120 <= duration <= 900 else "warn",
                {"seconds": duration, "range": [120, 900]},
                "dump stage duration",
            )
        )

    # --- log messages ---
    log_messages: list[dict[str, Any]] = []
    classified = []
    consoles = [
        s
        for s in stages
        if s.get("record") and s["record"].endswith(".console") and s["nn"] is not None
    ]
    final_verification = {s["nn"] for s in (split_stage, hash_stage, info_stage) if s}
    latest_ok: dict[tuple[str, str | None], int] = {}
    for s in consoles:
        if s["kind"] == "acquisition" and s["exit_code"] == 0 and not s["interrupted"]:
            latest_ok[(s["name"], s["label"])] = s["nn"]
    for s in sorted(consoles, key=lambda s: s["nn"]):
        items = catalog.classify(s["record"], rec.lines(s["record"]))
        log_messages.extend(
            {
                "record": item.record,
                "line": item.line,
                "class": item.cls,
                "code": item.code,
            }
            for item in items
        )
        relevant = (
            s["kind"] == "acquisition" and s["exit_code"] == 0 and not s["interrupted"]
        ) or (s["nn"] in final_verification)
        if relevant:
            classified += items

    # --- issues ---
    for item in classified:
        if item.cls == "info":
            continue
        actions = [item.action] if item.action else []
        if item.acceptable:
            actions.append(accept(item.code))
        add(
            Issue(
                item.code,
                item.cls,
                f"redumper message {item.code}",
                f"{item.record}: {item.line}",
                item.why,
                actions,
                item.acceptable,
                {"record": item.record, "line": item.line},
            )
        )
    # counts exists only when state_path does; the check just tells mypy so.
    if counts and counts.scsi_samples and state_path:
        runs = rawdata.error_runs(
            state_path,
            rawdata.STATE_ERROR_SKIP,
            _reported(pregap_start, "pre-gap start"),
            _reported(read_offset, "read offset"),
        )
        add(
            Issue(
                "scsi-errors-remaining",
                "blocking",
                "unread samples remain",
                f"{counts.scsi_samples} samples were never read (SCSI errors) in"
                f" {len(runs)} run(s)",
                "unread samples would be stored as zero placeholders",
                [refine_cmd, accept("scsi-errors-remaining")],
                True,
                {
                    "samples": counts.scsi_samples,
                    "lba_runs": [list(r) for r in runs[:MAX_RUNS]],
                },
            )
        )
    if counts and counts.c2_samples:
        per_track = [
            {"track": t["number"], **t["errors"]}
            for t in tracks
            if t["errors"] and t["errors"]["c2_samples"]
        ]
        add(
            Issue(
                "c2-errors-remaining",
                "blocking",
                "C2 errors remain",
                f"{counts.c2_samples} samples still have C2 errors",
                "these samples are unreliable",
                [refine_cmd, accept("c2-errors-remaining")],
                True,
                {"samples": counts.c2_samples, "tracks": per_track},
            )
        )
    if not (leadin and leadin.stored and leadin.stored[2]):
        add(
            Issue(
                "leadin-not-verified",
                "blocking",
                "session 1 lead-in not captured",
                "the PLEXTOR lead-in of session 1 was not stored as verified"
                + (
                    f" (stored unverified {leadin.stored[:2]})"
                    if leadin and leadin.stored
                    else ""
                ),
                "the session 1 lead-in can only be captured with the medium in the"
                " drive",
                [
                    (
                        f"coa discard {holon} {medium} --yes, then "
                        f"coa archive {holon} {medium} again"
                    ),
                    accept("leadin-not-verified"),
                ],
                True,
                {"attempts": leadin.attempts if leadin else 0},
            )
        )
    for number in toc.sessions if toc else []:
        if number >= 2:
            add(
                Issue(
                    f"leadin-missing-session-{number}",
                    "blocking",
                    f"session {number} lead-in not captured",
                    f"redumper b753 does not capture the lead-in of session {number}",
                    "every session's lead-in is part of the medium",
                    [accept(f"leadin-missing-session-{number}")],
                    True,
                    {"session": number},
                )
            )
    if protection and protection != "none":
        add(
            Issue(
                "protection-detected",
                "blocking",
                "protection detected",
                f"redumper reports protection: {protection}",
                "protected discs need an approved profile",
                [accept("protection-detected")],
                True,
                {"protection": protection},
            )
        )
    if split and split.qtoc_present:
        add(
            Issue(
                "toc-qtoc-mismatch",
                "blocking",
                "TOC/QTOC mismatch",
                "split printed a final QTOC",
                "the TOC and the Q subchannel disagree about the disc layout",
                [accept("toc-qtoc-mismatch")],
                True,
                {},
            )
        )
    if split_stage and "--force-split" in split_stage["argv"]:
        add(
            Issue(
                "split-refused",
                "blocking",
                "split forced",
                "split ran with --force-split after the refusal was accepted",
                "split refused or forced; track files include sectors with read errors",
                [refine_cmd, accept("split-refused")],
                True,
                {
                    "track_errors": {k: list(v) for k, v in split.track_errors.items()}
                    if split
                    else {}
                },
            )
        )
    if split_stage and split_stage["exit_code"] != 0:
        if split and split.refused and "--force-split" not in split_stage["argv"]:
            add(
                Issue(
                    "split-refused",
                    "blocking",
                    "split refused",
                    "redumper split refused because of read errors",
                    "without a split there are no canonical track hashes",
                    [
                        refine_cmd,
                        f"{accept('split-refused')} (the next run uses --force-split)",
                    ],
                    True,
                    {
                        "track_errors": {
                            k: list(v) for k, v in split.track_errors.items()
                        }
                    },
                )
            )
        else:
            add(
                Issue(
                    "stage-failed",
                    "blocking",
                    "verification stage failed",
                    f"split (stage {split_stage['nn']:02d}) exited with status"
                    f" {split_stage['exit_code']}",
                    "stage incomplete",
                    [f"rerun: coa archive {holon} {medium}"],
                    False,
                    {"nn": split_stage["nn"]},
                )
            )
    for s in (hash_stage, info_stage):
        if s and (s["exit_code"] != 0 or s["interrupted"]):
            add(
                Issue(
                    "stage-failed",
                    "blocking",
                    "verification stage failed",
                    f"{s['name']} (stage {s['nn']:02d}) exited with status"
                    f" {s['exit_code']}",
                    "stage incomplete",
                    [f"rerun: coa archive {holon} {medium}"],
                    False,
                    {"nn": s["nn"]},
                )
            )
    if split_stage and split_stage["exit_code"] == 0 and not hash_stage:
        add(
            Issue(
                "stage-failed",
                "blocking",
                "verification incomplete",
                "hash did not run after split",
                "stage incomplete",
                [f"rerun: coa archive {holon} {medium}"],
                False,
                {},
            )
        )
    if q and strict_q and toc:
        boundaries = sorted(
            {i.lba for t in toc.tracks for i in t.indices if t.number != "AA"}
            | ({toc.leadout_lba} if toc.leadout_lba is not None else set())
        )
        hits = [
            [a, b, x] for a, b in q.runs for x in boundaries if a <= x and b >= x - 1
        ]
        if hits:
            add(
                Issue(
                    "q-at-index-change",
                    "blocking",
                    "bad Q at a track or index change",
                    f"{len(hits)} bad-Q run(s) contain or touch a track/index change",
                    "the position of the change is uncertain",
                    [
                        f"coa refine {holon} {medium} --retries 30 --refine-subchannel",
                        accept("q-at-index-change"),
                    ],
                    True,
                    {"runs": hits[:MAX_RUNS]},
                )
            )
        if len(q.mcn_valid) > 1:
            add(
                Issue(
                    "mcn-conflict",
                    "blocking",
                    "conflicting MCN",
                    f"valid Q frames give {len(q.mcn_valid)} MCNs",
                    "the disc's catalog number is uncertain",
                    [accept("mcn-conflict")],
                    True,
                    {"values": dict(q.mcn_valid)},
                )
            )
        if q.mcn_in_invalid_only:
            add(
                Issue(
                    "mcn-unconfirmed",
                    "blocking",
                    "MCN only in damaged Q frames",
                    "an MCN appears only in Q frames failing their checksum",
                    "the MCN cannot be confirmed from a valid subchannel frame",
                    [
                        f"coa refine {holon} {medium} --retries 30 --refine-subchannel",
                        accept("mcn-unconfirmed"),
                    ],
                    True,
                    {},
                )
            )
        for track, values in sorted(q.isrc_valid.items()):
            if len(values) > 1:
                code = f"isrc-conflict-{track:02d}"
                add(
                    Issue(
                        code,
                        "blocking",
                        "conflicting ISRC",
                        f"track {track}: valid Q frames give {len(values)} ISRCs",
                        "the track's ISRC is uncertain",
                        [accept(code)],
                        True,
                        {"values": dict(values)},
                    )
                )
        for track in sorted(q.isrc_in_invalid_only):
            code = f"isrc-unconfirmed-{track:02d}"
            add(
                Issue(
                    code,
                    "blocking",
                    "ISRC only in damaged Q frames",
                    f"track {track}: an ISRC appears only in Q frames failing"
                    " their checksum",
                    "the ISRC cannot be confirmed from a valid subchannel frame",
                    [
                        f"coa refine {holon} {medium} --retries 30 --refine-subchannel",
                        accept(code),
                    ],
                    True,
                    {"track": track},
                )
            )
    if q and q.total:
        add(
            Issue(
                "q-errors-remaining",
                "warning",
                "Q errors remain",
                f"{q.program} bad Q sector(s) in the program area, {q.leadout} in"
                f" the lead-out, {len(q.runs)} run(s), longest {q.longest_run}",
                "raw subchannel bytes are kept; only their Q content is untrusted",
                [],
                True,
                {
                    "program": q.program,
                    "leadout": q.leadout,
                    "runs": len(q.runs),
                    "longest": q.longest_run,
                },
            )
        )
    if descriptor and descriptor.recordable:
        add(
            Issue(
                "recordable-medium",
                "warning",
                "recordable medium",
                f"{descriptor.media_type} medium",
                "recordable media degrade",
                [],
                True,
                {},
            )
        )
    if descriptor and descriptor.disc_status == 1:
        add(
            Issue(
                "open-medium",
                "warning",
                "open medium",
                "the medium is not finalized",
                "archived as it is",
                [],
                True,
                {},
            )
        )
    if split:
        pao = split.pao
        add(
            Issue(
                "write-offset",
                "info",
                "write offset",
                f"write offset {write_offset:+d} "
                f"({split.offset_method_line or 'method not reported'})"
                + (
                    f"; Perfect Audio Offset (silence level {pao[0]}): {pao[1]}"
                    if pao
                    else ""
                ),
                "the offset only affects track files; not checked against"
                " Redump/AccurateRip in v1",
                [],
                True,
                {"write_offset": write_offset, "method": split.offset_method},
            )
        )
        if (
            split.universal_hash is None
            and split_stage
            and split_stage["exit_code"] == 0
        ):
            add(
                Issue(
                    "universal-hash-skipped",
                    "warning",
                    "Universal Hash skipped",
                    "redumper skipped the Universal Hash (non-zero data range not"
                    " continuous)",
                    "audio comparison falls back to per-track hashes",
                    [],
                    True,
                    {},
                )
            )
    for c in checks:
        if c["status"] != "fail":
            continue
        if c["id"].startswith("raw-unchanged"):
            add(
                Issue(
                    "raw-modified",
                    "blocking",
                    "raw files changed",
                    f"raw files changed during verification:"
                    f" {', '.join(c['values'].get('changed', []))}",
                    "the raw dump must stay as acquired",
                    [f"coa discard {holon} {medium} --yes and re-dump"],
                    False,
                    c["values"],
                )
            )
        else:
            code = f"check-failed-{c['id']}"
            add(
                Issue(
                    code,
                    "blocking",
                    f"consistency check {c['id']} failed",
                    f"{c['note']}: {c['values']}",
                    "unexplained mismatch — possible specification or"
                    " implementation issue",
                    [accept(code)],
                    True,
                    c["values"],
                )
            )

    issue_list = [
        i.as_dict() | {"accepted": i.code in accepted_codes and i.acceptable}
        for i in issues.values()
    ]
    return {
        "drive": drive,
        "medium": medium_doc,
        "remaining_errors": remaining,
        "lba_start": rawdata.LBA_START,
        "write_offset_samples": write_offset,
        "write_offset": {
            "method": split.offset_method if split else None,
            "method_line": split.offset_method_line if split else None,
            "perfect_audio_offset": (
                {
                    "silence_level": split.pao[0],
                    "ranges": [list(r) for r in split.pao[1]],
                }
                if split and split.pao
                else None
            ),
        },
        "nonzero_sample_ranges": {
            "toc": list(split.nonzero_toc_range)
            if split and split.nonzero_toc_range
            else None,
            "data": list(split.nonzero_data_range)
            if split and split.nonzero_data_range
            else None,
        },
        "universal_hash_sha1": split.universal_hash if split else None,
        "mcn": (q.mcn_first if q else None) or (cue.catalog if cue else None),
        "cd_text": [{"field": k, "value": v} for k, v in cue.cd_text] if cue else [],
        "sessions": sessions,
        "tracks": tracks,
        "raw_files": raw_files,
        "issues": issue_list,
        "log_messages": log_messages,
        "checks": checks,
    }


DERIVED_KEYS = (
    "drive",
    "medium",
    "remaining_errors",
    "lba_start",
    "write_offset_samples",
    "write_offset",
    "nonzero_sample_ranges",
    "universal_hash_sha1",
    "mcn",
    "cd_text",
    "sessions",
    "tracks",
    "raw_files",
    "issues",
    "log_messages",
    "checks",
)
