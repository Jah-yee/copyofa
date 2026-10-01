"""Synthetic raw files and schema-valid metadata for set/transaction tests (not real dumps)."""

import os
from pathlib import Path

from copyofa.archive import encoding
from copyofa.util import sha256_file

TS = "2026-10-01T00:00:00Z"


def raw_files(directory: Path, image: str, seed: int = 0) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    rnd = os.urandom if seed == 0 else (lambda n: bytes((i * 31 + seed) % 251 for i in range(n)))
    files = {
        f"{image}.scram": rnd(2352 * 10 - 120),
        f"{image}.state": bytes([4]) * ((2352 * 10 - 120) // 4),
        f"{image}.subcode": rnd(96 * 10),
        f"{image}.log": b"redumper (build: b753+d8fix)\n",
        f"{image}.stage-01-dump.console": b"progress\r" + b" " * 79 + b"\rmedia errors: \n",
    }
    result = {}
    for name, data in files.items():
        (directory / name).write_bytes(data)
        result[name] = directory / name
    return result


def metadata(holon: str, medium: str, raw: dict[str, Path], *, media_list=None, redundancy: bool = True,
             percent: int = 10) -> dict:
    import shlex

    from copyofa.archive.archiveset import par2_argv

    media_list = media_list or [medium]
    entries = [{"name": n, "size": p.stat().st_size, "sha256": sha256_file(p),
                "stored_as": encoding.stored_name(n, True)} for n, p in sorted(raw.items())]
    payload = [e["stored_as"] for e in entries]
    stage = {"nn": 1, "name": "dump", "kind": "acquisition", "label": None, "command_line": "redumper dump",
             "argv": ["redumper", "dump"], "started_at": TS, "ended_at": TS, "duration_s": 0.0, "exit_code": 0,
             "interrupted": False, "record": f"{holon}-{medium}.stage-01-dump.console", "cached_at": None}
    return {
        "format_version": "0.1",
        "schema_version": "0.1",
        "holon_id": holon,
        "medium_id": medium,
        "position": media_list.index(medium) + 1,
        "media_list": media_list,
        "partial": None,
        "created_at": TS,
        "outcome": "PASS WITH ACCEPTED ISSUES",
        "tool": {"name": "coa", "version": "0.1.0", "host_os": {"name": "macOS", "version": "15", "machine": "arm64"},
                 "python": "3.14", "dependencies": {"redumper": "b753+d8fix", "flac": None, "xz": None, "zstd": None,
                                                    "par2": None}, "config": {}},
        "profile": {"name": "cd-audio-pressed", "version": "0.1", "status": "draft", "approved": False,
                    "permanence": "provisional", "reason": "synthetic", "stages": []},
        "drive": {"vendor": "PLEXTOR", "product": "CD-R PX-W5224A", "firmware": "1.04", "vendor_specific": None,
                  "drive_type": "PLEXTOR", "read_offset": 30, "c2_shift": 294, "pregap_start": -75,
                  "read_method": "D8", "sector_order": "DATA_C2_SUB", "configuration_line": None,
                  "connection": {"interconnect": "USB", "bridge": None},
                  "drive_test": {"record": None, "plextor_leadin": "yes"}},
        "medium": {"family": "CD", "media_type": "CD-ROM", "recordable": False, "rewritable": False, "layers": 1,
                   "disc_status": 2, "last_session_state": 3, "sessions": 1, "toc_source": "TOC", "protection": "none"},
        "stages": [stage],
        "resumes": [],
        "acquisition_errors": {"first_pass": None, "passes": [], "cumulative_max_attempts": {"c2_sectors": 1,
                                                                                            "q_sectors": 1}},
        "remaining_errors": {"scsi_samples": 0, "c2_samples": 0, "q_program_sectors": 0, "q_leadout_sectors": 0,
                             "q_runs": []},
        "lba_start": -45150,
        "write_offset_samples": 0,
        "write_offset": {"method": None, "method_line": None, "perfect_audio_offset": None},
        "nonzero_sample_ranges": {"toc": None, "data": None},
        "universal_hash_sha1": None,
        "mcn": None,
        "cd_text": [],
        "sessions": [],
        "tracks": [],
        "raw_files": entries,
        "redundancy": {"enabled": redundancy, "percent": percent if redundancy else None, "default_enabled": False,
                       "default_reason": "synthetic", "override": None,
                       "par2": ({"implementation": "par2cmdline", "version": None,
                                 "command": shlex.join(par2_argv(holon, medium, payload, percent))}
                                if redundancy else None)},
        "gaps": [],
        "accepted_issues": [],
        "issues": [],
        "log_messages": [],
        "checks": [],
        "external_verification": {"redump": {"result": "not checked"}, "accuraterip": {"result": "not checked"},
                                  "ctdb": {"result": "not checked"}},
        "revisions": [],
    }
