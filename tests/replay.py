"""Rebuild session records and recorded stage entries from a pilot dump made with
`redumper disc`.

The pilot dumps only have redumper's .log; each stage section becomes one console record
prefixed with the log's header, which is what the stage-by-stage runs of coa produce.
"""

import re
import shlex
from pathlib import Path
from typing import Any

from conftest import FIXTURES

STAGES = [
    ("DUMP", "dump", "dump"),
    ("DUMP::EXTRA", "dump::extra", "dump-extra"),
    ("PROTECTION", "protection", "protection"),
    ("REFINE", "refine", "refine"),
    ("SPLIT", "split", "split"),
    ("HASH", "hash", "hash"),
    ("INFO", "info", "info"),
]
RAW_EXTS = ("scram", "state", "subcode", "toc", "fulltoc", "cdtext", "atip", "pma")


def first_run(log_text: str) -> list[str]:
    lines = log_text.split("\n")
    end = next(
        (i for i, line in enumerate(lines[1:], 1) if line.startswith("=== ")),
        len(lines),
    )
    return lines[:end]


def build(
    pilot: Path, stem: str, image: str, records: Path, *, split_exit: int | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return (raw map, stage entries) for derive(); records are written into
    `records`."""
    records.mkdir(parents=True, exist_ok=True)
    log = (pilot / f"{stem}.log").read_text()
    lines = first_run(log)
    header = lines[1:15]
    sections: dict[str, list[str]] = {}
    current = None
    for line in lines:
        match = re.match(r"^\*\*\* (\S+) \(time check", line)
        if match:
            current = match.group(1)
            sections[current] = []
        elif current:
            sections[current].append(line)
    raw: dict[str, Path] = {}
    for ext in RAW_EXTS:
        if (pilot / f"{stem}.{ext}").exists():
            raw[f"{image}.{ext}"] = pilot / f"{stem}.{ext}"
    raw[f"{image}.log"] = pilot / f"{stem}.log"
    stages = []
    for name, cmd in [
        ("drutil status", "drutil"),
        ("ioreg", "ioreg"),
        ("drive::test", "drive-test"),
    ]:
        path = (
            records
            / f"{image}.preflight-{cmd}{'.console' if cmd == 'drive-test' else '.txt'}"
        )
        if cmd == "drutil":
            path.write_bytes((FIXTURES / "drutil" / "8W20X-status.xml").read_bytes())
        elif cmd == "ioreg":
            path.write_text(
                '"Vendor Identification" = "PLEXTOR"\n'
                '"Product Identification" = "CD-R   PX-W5224A"\n'
                '"Product Revision Level" = "1.04"\n'
                '"idVendor" = 5421\n"idProduct" = 9016\n'
                '"USB Product Name" = "USB to ATA/ATAPI bridge"\n'
            )
        else:
            path.write_text(
                "\n".join(header)
                + "\n*** DRIVE::TEST (time check: 0s)\n\nPLEXTOR lead-in: yes\n"
            )
        raw[path.name] = path
        stages.append(entry(None, name, "preflight", [cmd], path.name))
    nn = 0
    for key, command, record_stage in STAGES:
        if key not in sections:
            continue
        nn += 1
        path = records / f"{image}.stage-{nn:02d}-{record_stage}.console"
        path.write_text(
            "\n".join(
                header
                + [f"*** {key} (time check: 0s)"]
                + sections[key]
                + ["*** END (time check: 0s)", ""]
            )
        )
        raw[path.name] = path
        kind = "verification" if command in ("split", "hash", "info") else "acquisition"
        argv = ["redumper", command, "--verbose"] + (
            ["--leave-unchanged"] if command == "split" else []
        )
        exit_code = split_exit if (command == "split" and split_exit is not None) else 0
        stages.append(entry(nn, command, kind, argv, path.name, exit_code=exit_code))
        if command == "split" and exit_code != 0:
            break
    return raw, stages


def entry(
    nn: int | None,
    name: str,
    kind: str,
    argv: list[str],
    record: str,
    exit_code: int = 0,
) -> dict[str, Any]:
    return {
        "nn": nn,
        "name": name,
        "kind": kind,
        "label": None,
        "command_line": shlex.join(argv),
        "argv": argv,
        "started_at": "2026-09-29T18:04:54Z",
        "ended_at": "2026-09-29T18:09:24Z",
        "duration_s": 270.0,
        "exit_code": exit_code,
        "interrupted": False,
        "record": record,
        "cached_at": None,
    }


def recorded(
    stages: list[dict[str, Any]], accepted: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "stages": stages,
        "accepted_issues": accepted or [],
        "profile": {"strict_q": True},
        "checks": [],
        "acquisition_errors": {"first_pass": {"q_program_rate": 0.0005}},
    }
