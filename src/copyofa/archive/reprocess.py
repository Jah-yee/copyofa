"""`coa reprocess`: re-derive the metadata from the archived raw files and reseal."""

import copy
import json
import shutil
from pathlib import Path

import click

from .. import __version__
from ..acquisition.workstate import scratch_root
from ..config import Config
from ..errors import IntegrityFailure, UsageError
from ..ids import normalize_holon_id, parse_medium_id
from ..issues import Issue, open_blocking
from ..redumper.messages import Catalog
from ..system.macos import FileLock
from ..util import utc_compact, utc_now
from . import analysis, archiveset, encoding, metadata
from . import product as product_mod
from .transaction import Transaction, recover
from .verify import LOCK, fast_verify, medium_doc


def reprocess(
    cfg: Config,
    holon_text: str,
    medium_text: str,
    reason: str,
    *,
    redundancy: str | None = None,
    redundancy_reason: str | None = None,
) -> int:
    holon = normalize_holon_id(holon_text)
    medium = str(parse_medium_id(medium_text))
    if not reason or not reason.strip():
        raise UsageError("reprocess needs a non-empty --reason")
    if redundancy is not None and not redundancy_reason:
        raise UsageError("--redundancy needs --redundancy-reason")
    pd = cfg.archive_root / holon
    if not (pd / "product.json").exists():
        raise UsageError(f"no product manifest in {pd}")
    with FileLock(pd / LOCK):
        recover(pd, cfg.work_root)
        product = product_mod.read_product(pd)
        entry = next((e for e in product["media"] if e["medium_id"] == medium), None)
        if entry is None or entry["status"] != "archived":
            raise UsageError(f"{holon} {medium} is not archived")
        mdir = product_mod.medium_dir(pd, product, medium)
        problems = fast_verify(mdir, holon, medium)
        if problems:
            raise IntegrityFailure(
                f"{holon} {medium} is damaged ({'; '.join(map(str, problems))}): "
                f"run coa verify {holon} {medium} --repair"
            )
        old = medium_doc(mdir, holon, medium)
        image = f"{holon}-{medium}"
        scratch = scratch_root(cfg.work_root) / f"reprocess-{image}-{utc_compact()}"
        scratch.mkdir()
        try:
            raw: dict[str, Path] = {}
            for f in old["raw_files"]:
                target = scratch / f["name"]
                size, digest = encoding.decode_to(mdir / f["stored_as"], target)
                if (size, digest) != (f["size"], f["sha256"]):
                    raise IntegrityFailure(
                        f"{f['stored_as']} does not rebuild {f['name']} bit-exactly"
                    )
                raw[f["name"]] = target
            derived = analysis.derive(
                holon,
                medium,
                raw,
                metadata.recorded_inputs(old),
                Catalog.load(cfg.user_messages_path),
            )
            new = copy.deepcopy(old)
            changes = []
            for key in analysis.DERIVED_KEYS:
                if json.dumps(derived[key], sort_keys=True) != json.dumps(
                    old[key], sort_keys=True
                ):
                    changes.append({"field": f"/{key}", "previous": old[key]})
                    new[key] = derived[key]
            if redundancy is not None:
                enabled = redundancy == "on"
                state = {
                    "default_enabled": old["redundancy"]["default_enabled"],
                    "default_reason": old["redundancy"]["default_reason"],
                    "override": {"value": enabled, "reason": redundancy_reason},
                }
                payload = [f["stored_as"] for f in new["raw_files"]]
                new["redundancy"] = metadata.redundancy_doc(
                    cfg, state, holon, medium, payload
                )
                if new["redundancy"] != old["redundancy"]:
                    changes.append(
                        {"field": "/redundancy", "previous": old["redundancy"]}
                    )
            new["format_version"] = metadata.FORMAT_VERSION
            new["revisions"].append(
                {
                    "revised_at": utc_now(),
                    "operation": "reprocess",
                    "reason": reason.strip(),
                    "tool_version": __version__,
                    "format_version": metadata.FORMAT_VERSION,
                    "changes": changes,
                }
            )
            metadata.validate(new, "medium")
            new_product = copy.deepcopy(product)
            for e in new_product["media"]:
                if e["medium_id"] == medium:
                    e["format_version"] = new["format_version"]
            same_names = [f["stored_as"] for f in new["raw_files"]] == [
                f["stored_as"] for f in old["raw_files"]
            ]
            stored = {f["stored_as"]: mdir / f["stored_as"] for f in old["raw_files"]}

            def build(staging: Path) -> None:
                if same_names:
                    archiveset.build_set(staging, metadata=new, stored=stored)
                else:
                    archiveset.build_set(staging, metadata=new, raw=raw)
                product_mod.write_product_files(staging, new_product)

            Transaction(pd, "reseal", holon, medium, cfg=cfg).run(build)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    click.echo(f"resealed {image}: {len(changes)} change(s)")
    for change in changes:
        click.echo(f"  changed {change['field']}")
    issues = [Issue.from_dict(i) for i in new["issues"]]
    blocking = open_blocking(issues, new["accepted_issues"])
    if blocking:
        click.echo(
            f"note: {len(blocking)} blocking issue(s) are not accepted under the "
            f"current rules: {', '.join(i.code for i in blocking)} (the recorded "
            "outcome is unchanged)"
        )
    return 0
