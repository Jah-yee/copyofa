"""Lossless, deterministic payload encodings; the per-file-type choices are fixed
here."""

import hashlib
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..errors import IntegrityFailure
from ..util import CHUNK


def encoding_for(raw_name: str, audio_scram: bool) -> str:
    """`.flac` for the .scram of audio-only CDs, `.zst` for .state, `.xz` for
    everything else.
    """
    if raw_name.endswith(".scram"):
        return "flac" if audio_scram else "xz"
    if raw_name.endswith(".state"):
        return "zst"
    return "xz"


def stored_name(raw_name: str, audio_scram: bool) -> str:
    return f"{raw_name}.{encoding_for(raw_name, audio_scram)}"


def _encode_argv(kind: str, src: Path, dst: Path) -> tuple[list[str], bool]:
    """(argv, writes to stdout)."""
    if kind == "flac":
        return [
            "flac",
            "-8",
            "--silent",
            "--no-preserve-modtime",
            "--force-raw-format",
            "--endian=little",
            "--sign=signed",
            "--channels=2",
            "--bps=16",
            "--sample-rate=44100",
            "-o",
            str(dst),
            str(src),
        ], False
    if kind == "xz":
        return ["xz", "-9e", "-T1", "-c", str(src)], True
    if kind == "zst":
        return [
            "zstd",
            "--ultra",
            "-22",
            "--long=31",
            "-T1",
            "-q",
            "-c",
            str(src),
        ], True
    raise ValueError(kind)


def decode_argv(stored: Path) -> list[str]:
    name = stored.name
    if name.endswith(".flac"):
        return [
            "flac",
            "-d",
            "-c",
            "--silent",
            "--force-raw-format",
            "--endian=little",
            "--sign=signed",
            str(stored),
        ]
    if name.endswith(".xz"):
        return ["xz", "-dc", str(stored)]
    if name.endswith(".zst"):
        return ["zstd", "-dc", "--long=31", str(stored)]
    raise ValueError(f"{name}: unknown encoding")


def encode(src: Path, dst: Path, kind: str) -> None:
    argv, to_stdout = _encode_argv(kind, src, dst)
    if to_stdout:
        with dst.open("xb") as out:
            proc = subprocess.run(argv, stdout=out, stderr=subprocess.PIPE, check=False)
    else:
        if src.stat().st_size == 0:
            raise IntegrityFailure(
                f"{src.name}: empty audio stream cannot be stored as FLAC"
            )
        proc = subprocess.run(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False
        )
    if proc.returncode != 0:
        raise IntegrityFailure(
            f"encoding {src.name} failed: {' '.join(argv)}: "
            f"{proc.stderr.decode(errors='replace')}"
        )
    dst.chmod(0o644)  # flac copies the read-only mode of the raw input


def decode_to(stored: Path, dst: Path | None = None) -> tuple[int, str]:
    """Stream-decode a stored file; returns (size, sha256) and optionally writes
    the raw file.
    """
    argv = decode_argv(stored)
    digest = hashlib.sha256()
    size = 0
    out = dst.open("xb") if dst is not None else None
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert proc.stdout is not None
        while chunk := proc.stdout.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
            if out is not None:
                out.write(chunk)
        stderr = proc.stderr.read() if proc.stderr else b""
        if proc.wait() != 0:
            raise IntegrityFailure(
                f"decoding {stored.name} failed: "
                f"{stderr.decode(errors='replace').strip()}"
            )
    finally:
        if out is not None:
            out.close()
    return size, digest.hexdigest()


def encode_all(jobs: list[tuple[Path, Path, str]]) -> None:
    """Encode (src, dst, kind) jobs, four at a time."""
    with ThreadPoolExecutor(max_workers=4) as pool:
        for future in [pool.submit(encode, *job) for job in jobs]:
            future.result()


def tool_versions() -> dict[str, str | None]:
    def first_line(argv: list[str]) -> str | None:
        if not shutil.which(argv[0]):
            return None
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)
        text = (proc.stdout or proc.stderr).strip()
        return text.splitlines()[0] if text else None

    return {
        "flac": first_line(["flac", "--version"]),
        "xz": first_line(["xz", "--version"]),
        "zstd": first_line(["zstd", "--version"]),
        "par2": first_line(["par2", "--version"]),
    }
