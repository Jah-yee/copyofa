"""Build a medium's file set in its fixed creation order: payload, metadata,
PAR2, checksum file.
"""

import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..errors import IntegrityFailure
from ..util import dumps_json, sha256_file
from . import encoding
from . import metadata as metadata_mod


def par2_argv(
    holon: str, medium: str, payload_names: list[str], percent: int
) -> list[str]:
    return [
        "par2",
        "create",
        "-q",
        f"-r{percent}",
        f"{holon}-{medium}.par2",
        *sorted(payload_names),
        f"{holon}-{medium}.json",
    ]


def medium_files(directory: Path, holon: str, medium: str) -> list[Path]:
    prefix = f"{holon}-{medium}."
    return sorted(
        p for p in directory.iterdir() if p.is_file() and p.name.startswith(prefix)
    )


def write_checksums(directory: Path, holon: str, medium: str) -> None:
    """Group 4: `shasum -a 256` lines for every other file of the medium,
    sorted by name.
    """
    checksum = directory / f"{holon}-{medium}.sha256"
    names = [
        p.name
        for p in medium_files(directory, holon, medium)
        if p != checksum and not p.name.endswith(".journal")
    ]
    checksum.write_text(
        "".join(f"{sha256_file(directory / n)}  {n}\n" for n in names), encoding="utf-8"
    )


def build_set(
    staging: Path,
    *,
    metadata: dict[str, Any],
    raw: dict[str, Path] | None = None,
    stored: dict[str, Path] | None = None,
) -> list[str]:
    """Create the complete medium set inside `staging`; exactly one of raw
    (encode) or stored (copy) is given.
    """
    if (raw is None) == (stored is None):
        raise ValueError("build_set needs exactly one of raw or stored")
    holon, medium = metadata["holon_id"], metadata["medium_id"]
    staging.mkdir(parents=True, exist_ok=True)
    payload = [f["stored_as"] for f in metadata["raw_files"]]
    metadata_mod.validate(metadata, "medium")
    if raw is not None:
        jobs = [
            (raw[f["name"]], staging / f["stored_as"], f["stored_as"].rsplit(".", 1)[1])
            for f in metadata["raw_files"]
        ]
        encoding.encode_all(jobs)
    else:
        for name in payload:
            shutil.copyfile(stored[name], staging / name)  # type: ignore[index]
    (staging / f"{holon}-{medium}.json").write_text(
        dumps_json(metadata), encoding="utf-8"
    )
    redundancy = metadata["redundancy"]
    if redundancy["enabled"]:
        argv = par2_argv(holon, medium, payload, redundancy["percent"])
        proc = subprocess.run(
            argv, cwd=staging, capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            raise IntegrityFailure(
                f"par2 create failed ({proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
    write_checksums(staging, holon, medium)
    return [p.name for p in medium_files(staging, holon, medium)]
