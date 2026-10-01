"""Transaction scenarios shared by test_transaction.py and its crash child process."""

import hashlib
import shutil
from collections.abc import Callable
from pathlib import Path

import synthetic as synthetic
from copyofa import vocabulary
from copyofa.archive import archiveset as archiveset
from copyofa.archive import product
from copyofa.archive.transaction import Transaction as Transaction
from copyofa.archive.transaction import checksum_only_verifier

H = "8W20X"
VOCAB = vocabulary.parse(vocabulary.default_text(), Path("media-prefixes.toml"))
KINDS = ("publish-flat", "publish-subdir", "reseal", "amend")


def file_map(pd: Path) -> dict[str, str]:
    return {
        p.relative_to(pd).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(pd.rglob("*"))
        if p.is_file() and p.name != "product.lock"
    }


def _publish(
    pd: Path,
    root: Path,
    medium: str,
    media: list[str],
    seed: int,
    hook: Callable[[str], None] | None = None,
) -> None:
    raw = synthetic.raw_files(root / f"raw-{medium}-{seed}", f"{H}-{medium}", seed=seed)
    doc = synthetic.metadata(H, medium, raw, media_list=media, redundancy=False)
    old = (
        product.read_product(pd)
        if (pd / "product.json").exists()
        else product.new_product(H, media, VOCAB)
    )
    new = product.with_archived(old, medium, doc)

    def build(staging: Path) -> None:
        archiveset.build_set(staging, metadata=doc, raw=raw)
        product.write_product_files(staging, new)

    Transaction(
        pd,
        "publish",
        H,
        medium,
        staged_verifier=checksum_only_verifier,
        crash_hook=hook,
    ).run(build)


def setup(kind: str, root: Path) -> Path:
    """Create the old state of a scenario; returns the product dir."""
    pd = root / "archive" / H
    pd.mkdir(parents=True)
    if kind == "publish-subdir":
        _publish(pd, root, "CD1", ["CD1", "CD2"], 1)
    elif kind in ("reseal", "amend"):
        _publish(pd, root, "CD1", ["CD1"], 1)
    return pd


def act(
    kind: str, root: Path, pd: Path, hook: Callable[[str], None] | None = None
) -> None:
    """Perform the scenario's transaction."""
    if kind == "publish-flat":
        _publish(pd, root, "CD1", ["CD1"], 2, hook)
    elif kind == "publish-subdir":
        _publish(pd, root, "CD2", ["CD1", "CD2"], 2, hook)
    elif kind == "reseal":
        doc = product.read_product(pd)
        meta_path = pd / f"{H}-CD1.json"
        import json

        meta = json.loads(meta_path.read_text())
        meta["revisions"].append(
            {
                "revised_at": synthetic.TS,
                "operation": "reprocess",
                "reason": "test",
                "tool_version": "0.1.0",
                "format_version": "0.1",
                "changes": [],
            }
        )
        stored = {f["stored_as"]: pd / f["stored_as"] for f in meta["raw_files"]}

        def build(staging: Path) -> None:
            archiveset.build_set(staging, metadata=meta, stored=stored)
            product.write_product_files(staging, doc)

        Transaction(
            pd,
            "reseal",
            H,
            "CD1",
            staged_verifier=checksum_only_verifier,
            crash_hook=hook,
        ).run(build)
    elif kind == "amend":
        old = product.read_product(pd)
        new = product.amended(old, "CD1,CD2", "test", VOCAB)
        new["amendments"][-1]["date"] = (
            synthetic.TS
        )  # deterministic, so old/new file maps are exact
        relocations = [
            (p.name, f"CD1/{p.name}")
            for p in sorted(pd.iterdir())
            if p.name.startswith(f"{H}-CD1.")
        ]
        Transaction(
            pd,
            "amend",
            H,
            None,
            staged_verifier=checksum_only_verifier,
            crash_hook=hook,
            relocations=relocations,
        ).run(lambda staging: product.write_product_files(staging, new))


def fresh(kind: str, root: Path) -> Path:
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    return setup(kind, root)
