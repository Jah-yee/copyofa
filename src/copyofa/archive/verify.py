"""`coa verify`: fast and deep verification, PAR2 repair."""

import json
import re
from dataclasses import dataclass
from pathlib import Path

import click

from ..errors import IntegrityFailure
from ..util import sha256_file
from . import metadata
from . import product as product_mod

LOCK = "product.lock"
PRODUCT_FILES = ("product.json", "product.sha256", LOCK)


@dataclass(frozen=True)
class Problem:
    kind: str  # damage | uncommitted | unexpected | layout
    where: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.where}: {self.detail}"


def read_checksums(path: Path) -> list[tuple[str, str]]:
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match:
            raise ValueError(f"malformed line in {path.name}: {line!r}")
        entries.append((match.group(1), match.group(2)))
    return entries


def fast_verify(medium_dir: Path, holon: str, medium: str, *, allow_journal: bool = False) -> list[Problem]:
    prefix = f"{holon}-{medium}"
    where = str(medium_dir / prefix)
    problems: list[Problem] = []
    if (medium_dir / f"{prefix}.journal").exists() and not allow_journal:
        problems.append(Problem("uncommitted", where, "a publication or reseal journal is present: run coa recover"))
    checksum = medium_dir / f"{prefix}.sha256"
    if not checksum.exists():
        problems.append(Problem("damage", where, f"{checksum.name} is missing"))
        return problems
    try:
        entries = read_checksums(checksum)
    except ValueError as exc:
        return problems + [Problem("damage", where, str(exc))]
    listed = set()
    for digest, name in entries:
        listed.add(name)
        path = medium_dir / name
        if not path.is_file():
            problems.append(Problem("damage", where, f"{name} is missing"))
        elif sha256_file(path) != digest:
            problems.append(Problem("damage", where, f"{name} fails its checksum"))
    for path in sorted(medium_dir.iterdir()):
        if path.name.startswith(f"{prefix}.") and path.name not in listed and path != checksum and not (
            path.name == f"{prefix}.journal"
        ):
            problems.append(Problem("unexpected", where, f"{path.name} is not listed in {checksum.name}"))
    return problems


def medium_doc(medium_dir: Path, holon: str, medium: str) -> dict:
    return json.loads((medium_dir / f"{holon}-{medium}.json").read_text(encoding="utf-8"))


def verify_product(product_dir: Path, *, allow_journal: bool = False) -> list[Problem]:
    """Product files, layout and manifest-vs-metadata consistency."""
    problems: list[Problem] = []
    where = str(product_dir)
    for path in product_dir.iterdir():
        if path.name.startswith((".coa-staging-", ".coa-backup-")):
            problems.append(Problem("uncommitted", where, f"leftover {path.name}: run coa recover"))
    journals = [p for p in [product_dir / "product.journal", *product_dir.glob("*.journal"),
                            *product_dir.glob("*/*.journal")] if p.exists()]
    if journals and not allow_journal:
        for journal in sorted(set(journals)):
            problems.append(Problem("uncommitted", where, f"{journal.relative_to(product_dir)}: run coa recover"))
    try:
        doc = product_mod.read_product(product_dir)
    except FileNotFoundError:
        return problems + [Problem("damage", where, "product.json is missing")]
    except (IntegrityFailure, json.JSONDecodeError) as exc:
        return problems + [Problem("damage", where, str(exc))]
    holon = doc["holon_id"]
    flat = product_mod.is_flat(doc)
    archived = {e["medium_id"]: e for e in doc["media"] if e["status"] == "archived"}
    allowed_dirs = set() if flat else set(archived)
    medium_prefixes = {f"{holon}-{m}." for m in archived} if flat else set()
    for path in sorted(product_dir.iterdir()):
        name = path.name
        if name in PRODUCT_FILES or name.startswith((".coa-staging-", ".coa-backup-")):
            continue
        if path.is_dir():
            if name not in allowed_dirs:
                problems.append(Problem("layout", where, f"unexpected directory {name}/"))
            continue
        if name == "product.journal" or any(name.startswith(p) for p in medium_prefixes):
            continue
        problems.append(Problem("layout", where, f"unexpected file {name}"))
    for medium, entry in archived.items():
        mdir = product_mod.medium_dir(product_dir, doc, medium)
        meta = mdir / f"{holon}-{medium}.json"
        if not meta.exists():
            problems.append(Problem("layout", where, f"{medium} is archived but {meta.relative_to(product_dir)} is missing"))
            continue
        if not flat:
            for path in mdir.iterdir():
                if not path.name.startswith(f"{holon}-{medium}."):
                    problems.append(Problem("layout", str(mdir), f"unexpected file {path.name}"))
        try:
            m = medium_doc(mdir, holon, medium)
        except json.JSONDecodeError as exc:
            problems.append(Problem("damage", str(mdir), f"metadata is not JSON: {exc}"))
            continue
        expected = {"permanence": m.get("profile", {}).get("permanence"), "format_version": m.get("format_version"),
                    "outcome": m.get("outcome")}
        actual = {k: entry.get(k) for k in expected}
        if expected != actual:
            problems.append(Problem("damage", where, f"product.json entry for {medium} {actual} differs from metadata {expected}"))
    return problems


def check_metadata_schema(doc: dict) -> list[str]:
    try:
        metadata.validate(doc, "medium")
    except IntegrityFailure as exc:
        return [str(exc)]
    return []


# --- deep verification ----------------------------------------------------------------------------

_INFIX = re.compile(r"\.(stage-\d+-[a-z-]+\.console|verify-\d+\.log|preflight-[a-z-]+\.(txt|console))$")


def deep_verify(medium_dir: Path, holon: str, medium: str, doc: dict, cfg, *, scratch_prefix: str = "deep") -> list[Problem]:
    """Deep level: rebuild every raw file, re-run split + hash, re-derive and compare the metadata."""
    import os
    import shutil
    import subprocess

    from ..acquisition.workstate import scratch_root
    from ..redumper import console
    from ..redumper.commands import build_argv
    from ..redumper.messages import Catalog
    from ..util import utc_compact
    from . import analysis, encoding

    image = f"{holon}-{medium}"
    where = str(medium_dir / image)
    problems = fast_verify(medium_dir, holon, medium)
    if any(p.kind in ("damage", "uncommitted") for p in problems):
        return problems
    problems += [Problem("damage", where, e) for e in check_metadata_schema(doc)]
    if problems:
        return problems
    scratch = scratch_root(cfg.work_root) / f"{scratch_prefix}-{image}-{utc_compact()}"
    raw_dir = scratch / "raw"
    raw_dir.mkdir(parents=True)
    try:
        raw: dict[str, Path] = {}
        for entry in doc["raw_files"]:
            target = raw_dir / entry["name"]
            size, digest = encoding.decode_to(medium_dir / entry["stored_as"], target)
            if (size, digest) != (entry["size"], entry["sha256"]):
                problems.append(Problem("damage", where, f"{entry['stored_as']} does not rebuild {entry['name']} "
                                                         "bit-exactly"))
            raw[entry["name"]] = target
        if doc["redundancy"]["enabled"]:
            proc = subprocess.run(["par2", "verify", "-q", f"{image}.par2"], cwd=medium_dir, capture_output=True)
            if proc.returncode != 0:
                problems.append(Problem("damage", where, f"par2 verify failed (exit {proc.returncode})"))
        if problems:
            return problems
        if doc["tracks"]:
            split_dir = scratch / "split"
            split_dir.mkdir()
            for name, path in raw.items():
                if not _INFIX.search(name) and not name.endswith(".log"):
                    os.symlink(path, split_dir / name)
            redumper = shutil.which(cfg.redumper_path) or cfg.redumper_path
            forced = any(a["code"] == "split-refused" for a in doc["accepted_issues"])
            options = ["--leave-unchanged"] + (["--force-split"] if forced else [])
            roms = {}
            for command, opts in (("split", options), ("hash", [])):
                argv = build_argv(redumper, command, image_path=split_dir, image_name=image, options=opts)
                proc = subprocess.run(argv, cwd=split_dir, capture_output=True)
                (scratch / f"{command}.console").write_bytes(proc.stdout + proc.stderr)
                if proc.returncode != 0:
                    problems.append(Problem("damage", where, f"redumper {command} on the rebuilt raw files failed"))
                    return problems
                if command == "hash":
                    roms = console.parse_hash(console.clean(proc.stdout))
            for track in doc["tracks"]:
                rom = roms.get(track["file_name"])
                actual = {"size": rom.size, "crc32": rom.crc32, "md5": rom.md5, "sha1": rom.sha1} if rom else None
                if actual != track["hashes"]:
                    problems.append(Problem("damage", where, f"track {track['number']}: re-split hashes differ"))
        from .metadata import recorded_inputs

        derived = analysis.derive(holon, medium, raw, recorded_inputs(doc), Catalog.load(cfg.user_messages_path))
        for key in analysis.DERIVED_KEYS:
            if json.dumps(derived[key], sort_keys=True) != json.dumps(doc[key], sort_keys=True):
                problems.append(Problem("damage", where, f"re-derived /{key} differs from the stored metadata"))
        names = {f["name"] for f in doc["raw_files"]}
        for stage in doc["stages"]:
            if stage["record"] not in names:
                problems.append(Problem("damage", where, f"stage record {stage['record']} is not in raw_files"))
        accepted_in_issues = {i["code"] for i in doc["issues"] if i["accepted"]}
        for acceptance in doc["accepted_issues"]:
            code = acceptance["code"]
            if code not in accepted_in_issues and code not in metadata.PRE_DUMP_DECISIONS:
                problems.append(Problem("damage", where, f"accepted issue {code} matches no accepted issue"))
        return problems
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# --- repair -------------------------------------------------------------------------------


def _passing(directory: Path, checksum: Path) -> set[str]:
    try:
        entries = read_checksums(checksum)
    except (ValueError, FileNotFoundError):
        return set()
    return {n for d, n in entries if (directory / n).is_file() and sha256_file(directory / n) == d}


def repair_medium(cfg, pd: Path, product: dict, holon: str, medium: str, *, staged_verifier=None) -> list[str]:
    """Repair a damaged medium from its PAR2 set on a scratch copy, then reseal it."""
    import shutil
    import subprocess

    from .. import __version__
    from ..acquisition.workstate import scratch_root
    from ..util import utc_compact, utc_now
    from . import archiveset
    from .transaction import Transaction

    image = f"{holon}-{medium}"
    mdir = product_mod.medium_dir(pd, product, medium)
    scratch = scratch_root(cfg.work_root) / f"repair-{image}-{utc_compact()}"
    scratch.mkdir()
    try:
        for path in mdir.iterdir():
            if path.is_file() and path.name.startswith(f"{image}.") and not path.name.endswith(".journal"):
                shutil.copyfile(path, scratch / path.name)
        checksum = scratch / f"{image}.sha256"
        before = _passing(scratch, checksum)
        try:
            listed = {n for _, n in read_checksums(checksum)}
        except (ValueError, FileNotFoundError):
            listed = set()
        damaged = sorted(listed - before)
        par2_files = sorted(p.name for p in scratch.glob(f"{image}*.par2"))
        if not par2_files:
            raise IntegrityFailure(f"{image}: no PAR2 files; unrecoverable: re-dump or restore from backup")
        intact = [n for n in par2_files if n in before]
        ordered = intact + [n for n in par2_files if n not in intact]
        repaired = False
        for name in ordered:
            proc = subprocess.run(["par2", "repair", "-q", name], cwd=scratch, capture_output=True)
            if proc.returncode == 0:
                repaired = True
                break
        if not repaired:
            raise IntegrityFailure(f"{image}: no PAR2 file could repair the damage; unrecoverable: re-dump or "
                                   "restore from backup")
        for path in scratch.iterdir():
            if path.name not in listed and re.search(r"\.\d+$", path.name):
                path.unlink()
        doc = json.loads((scratch / f"{image}.json").read_text(encoding="utf-8"))
        payload = [f["stored_as"] for f in doc["raw_files"]]
        if listed:
            still = sorted(n for n in set(payload + [f"{image}.json"]) & listed if n not in _passing(scratch, checksum))
            if still:
                raise IntegrityFailure(f"{image}: still damaged after repair: {', '.join(still)}; unrecoverable: "
                                       "re-dump or restore from backup")
        doc["revisions"].append({
            "revised_at": utc_now(),
            "operation": "repair",
            "reason": f"repaired {', '.join(damaged) or 'checksum file'}",
            "tool_version": __version__,
            "format_version": doc["format_version"],
            "changes": [],
        })
        stored = {name: scratch / name for name in payload}

        def build(staging: Path) -> None:
            archiveset.build_set(staging, metadata=doc, stored=stored)
            product_mod.write_product_files(staging, product)

        Transaction(pd, "reseal", holon, medium, cfg=cfg, staged_verifier=staged_verifier).run(build)
        return damaged
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# --- command --------------------------------------------------------------------------------------


def verify_command(cfg, holon_text: str, medium_text: str | None = None, *, deep: bool = False,
                   repair: bool = False) -> int:
    import contextlib

    from ..acquisition.workstate import product_marker
    from ..errors import EXIT_INTEGRITY, EXIT_PASS, EXIT_UNCOMMITTED, UsageError
    from ..ids import normalize_holon_id, parse_medium_id
    from ..system.macos import FileLock
    from .transaction import recover

    holon = normalize_holon_id(holon_text)
    only = str(parse_medium_id(medium_text)) if medium_text else None
    pd = cfg.archive_root / holon
    if not pd.is_dir():
        raise UsageError(f"no product directory {pd}")
    with contextlib.ExitStack() as stack:
        if repair:
            stack.enter_context(FileLock(pd / LOCK))
            recover(pd, cfg.work_root)
        problems = verify_product(pd)
        try:
            doc = product_mod.read_product(pd)
        except Exception:  # noqa: BLE001 - already reported by verify_product
            doc = None
        if doc is not None:
            if only and only not in [e["medium_id"] for e in doc["media"]]:
                raise UsageError(f"{only} is not in the media list of {holon}")
            for entry in doc["media"]:
                medium = entry["medium_id"]
                if only and medium != only:
                    continue
                if entry["status"] != "archived":
                    click.echo(f"{holon} {medium}: {entry['status']}")
                    continue
                mdir = product_mod.medium_dir(pd, doc, medium)
                found = fast_verify(mdir, holon, medium)
                if repair and found and not any(p.kind == "uncommitted" for p in found):
                    try:
                        fixed = repair_medium(cfg, pd, doc, holon, medium)
                        click.echo(f"{holon} {medium}: repaired {', '.join(fixed) or 'checksum file'}")
                        doc = product_mod.read_product(pd)
                        mdir = product_mod.medium_dir(pd, doc, medium)
                        found = fast_verify(mdir, holon, medium)
                    except IntegrityFailure as exc:
                        found.append(Problem("damage", str(mdir), str(exc)))
                if deep and not found:
                    found = deep_verify(mdir, holon, medium, medium_doc(mdir, holon, medium), cfg)
                partial = ""
                if not found:
                    meta = medium_doc(mdir, holon, medium)
                    if meta.get("partial"):
                        partial = f" partial ({meta['partial']['prefix']})"
                status = "OK" if not found else "PROBLEMS"
                click.echo(f"{holon} {medium}: {status}{' (deep)' if deep and not found else ''}{partial}")
                for problem in found:
                    click.echo(f"  {problem}")
                problems += found
            click.echo(product_mod.completeness_line(doc))
        for problem in [p for p in problems if p.where == str(pd)]:
            click.echo(f"  {problem}")
    if any(p.kind == "uncommitted" for p in problems):
        return EXIT_UNCOMMITTED
    if problems:
        return EXIT_INTEGRITY
    if only is None:
        marker = product_marker(cfg.work_root, holon)
        if marker.exists():
            marker.unlink()
    return EXIT_PASS
