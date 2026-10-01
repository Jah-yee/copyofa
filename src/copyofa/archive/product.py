"""Product manifest `product.json` + `product.sha256` and the product layout."""

import copy
import json
from pathlib import Path

from .. import vocabulary
from ..errors import IntegrityFailure, UsageError
from ..ids import parse_medium_id, validate_media_list
from ..util import dumps_json, sha256_bytes, utc_now
from . import metadata

FORMAT_VERSION = "0.1"
KNOWN_FORMATS = ("0.1",)


def read_product(product_dir: Path) -> dict:
    path, checksum = product_dir / "product.json", product_dir / "product.sha256"
    data = path.read_bytes()
    expected = f"{sha256_bytes(data)}  product.json\n"
    if not checksum.exists() or checksum.read_text(encoding="utf-8") != expected:
        raise IntegrityFailure(f"{checksum} does not match product.json")
    doc = json.loads(data)
    if doc.get("format_version") not in KNOWN_FORMATS:
        raise IntegrityFailure(f"{path}: unknown format version {doc.get('format_version')!r}")
    metadata.validate(doc, "product")
    return doc


def write_product_files(directory: Path, doc: dict) -> None:
    metadata.validate(doc, "product")
    data = dumps_json(doc).encode("utf-8")
    (directory / "product.json").write_bytes(data)
    (directory / "product.sha256").write_text(f"{sha256_bytes(data)}  product.json\n", encoding="utf-8")


def _entry(medium_id: str, vocab: vocabulary.Vocabulary) -> dict:
    handled = vocab.prefixes[parse_medium_id(medium_id).prefix].handled_by
    return {"medium_id": medium_id, "handled_by": handled,
            "status": "pending" if handled == "coa" else "handled elsewhere"}


def new_product(holon: str, media_list: list[str], vocab: vocabulary.Vocabulary) -> dict:
    return {"format_version": FORMAT_VERSION, "holon_id": holon,
            "media": [_entry(m, vocab) for m in media_list], "amendments": []}


def with_archived(product: dict, medium_id: str, medium_doc: dict) -> dict:
    doc = copy.deepcopy(product)
    for entry in doc["media"]:
        if entry["medium_id"] == medium_id:
            entry.update({
                "status": "archived",
                "permanence": medium_doc["profile"]["permanence"],
                "format_version": medium_doc["format_version"],
                "archived_at": medium_doc["created_at"],
                "outcome": medium_doc["outcome"],
            })
            return doc
    raise UsageError(f"{medium_id} is not in the media list of {product['holon_id']}")


def amended(product: dict, new_list: str, reason: str, vocab: vocabulary.Vocabulary) -> dict:
    if not reason or not reason.strip():
        raise UsageError("an amendment needs a non-empty --reason")
    media = [str(m) for m in validate_media_list(new_list, vocab)]
    old = {e["medium_id"]: e for e in product["media"]}
    lost = [m for m, e in old.items() if e["status"] == "archived" and m not in media]
    if lost:
        raise UsageError(f"archived media cannot be removed from the media list: {', '.join(lost)}")
    doc = copy.deepcopy(product)
    doc["media"] = [copy.deepcopy(old[m]) if m in old else _entry(m, vocab) for m in media]
    doc["amendments"].append({"date": utc_now(), "before": [e["medium_id"] for e in product["media"]],
                              "after": media, "reason": reason.strip()})
    return doc


def is_flat(product: dict) -> bool:
    return len(product["media"]) == 1


def medium_dir(product_dir: Path, product: dict, medium_id: str) -> Path:
    return product_dir if is_flat(product) else product_dir / medium_id


def completeness(product: dict) -> tuple[int, int, int]:
    coa_media = [e for e in product["media"] if e["handled_by"] == "coa"]
    permanent = sum(1 for e in coa_media if e["status"] == "archived" and e["permanence"] == "permanent")
    provisional = sum(1 for e in coa_media if e["status"] == "archived" and e["permanence"] == "provisional")
    return permanent, len(coa_media), provisional


def completeness_line(product: dict) -> str:
    permanent, total, provisional = completeness(product)
    line = f"{product['holon_id']}: {permanent} of {total} optical media archived"
    if provisional:
        line += f", {provisional} provisional"
    if permanent < total:
        line += " (incomplete)"
    return line


def amend(cfg, holon_text: str, media_list: str, reason: str) -> int:
    """`coa amend`: change the media list through an amend transaction."""
    from ..acquisition.workstate import product_marker
    from ..ids import normalize_holon_id
    from ..system.macos import FileLock
    from ..util import atomic_write_json
    from .transaction import Transaction, recover

    vocab = vocabulary.load(cfg.archive_root)
    holon = normalize_holon_id(holon_text)
    pd = cfg.archive_root / holon
    if not (pd / "product.json").exists():
        raise UsageError(f"{holon} has no product manifest yet; declare its media with coa archive --media-list")
    with FileLock(pd / "product.lock"):
        recover(pd, cfg.work_root)
        old = read_product(pd)
        new = amended(old, media_list, reason, vocab)
        relocations = []
        if is_flat(old) != is_flat(new):
            for entry in old["media"]:
                if entry["status"] != "archived":
                    continue
                medium = entry["medium_id"]
                src_dir, dst_dir = medium_dir(pd, old, medium), medium_dir(pd, new, medium)
                for path in sorted(src_dir.iterdir()):
                    if path.is_file() and path.name.startswith(f"{holon}-{medium}."):
                        relocations.append(((src_dir / path.name).relative_to(pd).as_posix(),
                                            (dst_dir / path.name).relative_to(pd).as_posix()))
        Transaction(pd, "amend", holon, None, cfg=cfg, relocations=relocations).run(
            lambda staging: write_product_files(staging, new)
        )
        if relocations:
            marker = product_marker(cfg.work_root, holon)
            marker.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(marker, {"verify_required_since": utc_now()})
            print(f"layout changed: run coa verify {holon} before archiving further media")
    print(f"media list of {holon}: {', '.join(e['medium_id'] for e in new['media'])}")
    print(completeness_line(new))
    return 0
