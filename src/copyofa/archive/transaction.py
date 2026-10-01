"""Journaled publication, reseal and media-list amendment with crash recovery.

The journal alone is the transaction state; unlinking it is the commit. Every step is a single rename, done when its
source is absent and its destination present. `recover()` rolls an interrupted transaction forward when every file
it installs still exists intact (at its source or destination), otherwise back.
"""

import json
import os
import shutil
from pathlib import Path
from typing import Callable

from ..errors import IntegrityFailure
from ..util import atomic_write_json, fsync_dir, sha256_file, utc_compact, utc_now
from . import product as product_mod

JOURNAL_VERSION = 1
STAGING_PREFIX = ".coa-staging-"
BACKUP_PREFIX = ".coa-backup-"


def _say(message: str) -> None:
    print(message, flush=True)


def checksum_only_verifier(staging: Path, holon: str, medium: str | None) -> list:
    from .verify import fast_verify

    problems = []
    if medium is not None:
        problems += fast_verify(staging, holon, medium)
    try:
        product_mod.read_product(staging)
    except Exception as exc:  # noqa: BLE001 - reported as a problem
        problems.append(str(exc))
    return problems


def deep_verifier(cfg) -> Callable:
    """Staged-set verifier: product files plus deep verification of the medium set."""

    def verifier(staging: Path, holon: str, medium: str | None) -> list:
        from .verify import deep_verify, medium_doc

        problems = checksum_only_verifier(staging, holon, None)
        if medium is not None:
            problems += deep_verify(staging, holon, medium, medium_doc(staging, holon, medium), cfg,
                                    scratch_prefix="staged")
        return problems

    return verifier


class Transaction:
    def __init__(self, product_dir: Path, operation: str, holon: str, medium: str | None, *,
                 cfg=None, staged_verifier: Callable | None = None,
                 crash_hook: Callable[[str], None] | None = None, relocations: list[tuple[str, str]] = ()) -> None:
        if operation not in ("publish", "reseal", "amend"):
            raise ValueError(operation)
        self.product_dir = product_dir
        self.operation = operation
        self.holon = holon
        self.medium = medium
        self.relocations = list(relocations)
        if staged_verifier is None:
            if medium is not None and cfg is None:
                raise ValueError("a medium transaction needs cfg for deep verification of the staged set")
            staged_verifier = deep_verifier(cfg) if medium is not None else checksum_only_verifier
        self.staged_verifier = staged_verifier
        self.crash_hook = crash_hook or (lambda label: None)
        stamp = utc_compact()
        name = f"{holon}-{medium or 'product'}-{stamp}"
        self.staging = product_dir / f"{STAGING_PREFIX}{name}"
        self.backup = product_dir / f"{BACKUP_PREFIX}{name}"

    def run(self, build: Callable[[Path], None]) -> None:
        pd = self.product_dir
        self.crash_hook("staged")
        self.staging.mkdir()
        build(self.staging)
        problems = self.staged_verifier(self.staging, self.holon, self.medium)
        if problems:
            shutil.rmtree(self.staging, ignore_errors=True)
            raise IntegrityFailure("staged set failed verification: " + "; ".join(str(p) for p in problems))
        _say("staged")
        new_product = json.loads((self.staging / "product.json").read_text(encoding="utf-8"))
        self.crash_hook("journal")
        created_dirs = []
        steps: list[dict] = []
        if self.medium is not None:
            target = product_mod.medium_dir(pd, new_product, self.medium)
            if not target.exists():
                target.mkdir()
                fsync_dir(pd)
                created_dirs.append(target.relative_to(pd).as_posix())
            journal = target / f"{self.holon}-{self.medium}.journal"
            prefix = f"{self.holon}-{self.medium}."
            old = sorted(p for p in target.iterdir() if p.is_file() and p.name.startswith(prefix)
                         and not p.name.endswith(".journal"))
            for path in old:
                steps.append({"kind": "backup", "src": self._rel(path), "dst": self._rel(self.backup / path.name)})
        else:
            journal = pd / "product.journal"
            target = pd
        for name in ("product.json", "product.sha256"):
            if (pd / name).exists():
                steps.append({"kind": "backup", "src": name, "dst": self._rel(self.backup / name)})
        for src, dst in self.relocations:
            dst_parent = (pd / dst).parent
            if not dst_parent.exists():
                dst_parent.mkdir()
                fsync_dir(pd)
                created_dirs.append(dst_parent.relative_to(pd).as_posix())
            steps.append({"kind": "relocate", "src": src, "dst": dst})
        if self.medium is not None:
            prefix = f"{self.holon}-{self.medium}."
            staged = sorted(p.name for p in self.staging.iterdir() if p.name.startswith(prefix))
            checksum = f"{prefix}sha256"
            staged = [n for n in staged if n != checksum] + [checksum]
            for name in staged:
                steps.append({"kind": "install", "src": self._rel(self.staging / name),
                              "dst": self._rel(target / name), "sha256": sha256_file(self.staging / name)})
        for name in ("product.json", "product.sha256"):
            steps.append({"kind": "install", "src": self._rel(self.staging / name), "dst": name,
                          "sha256": sha256_file(self.staging / name)})
        doc = {
            "journal_version": JOURNAL_VERSION,
            "operation": self.operation,
            "holon_id": self.holon,
            "medium_id": self.medium,
            "created_at": utc_now(),
            "staging_dir": self._rel(self.staging),
            "backup_dir": self._rel(self.backup),
            "created_dirs": created_dirs,
            "steps": steps,
        }
        self.backup.mkdir()
        atomic_write_json(journal, doc)
        _say("journal written")
        _say("installing")
        _forward(pd, journal, doc, self.crash_hook)


    def _rel(self, path: Path) -> str:
        return path.relative_to(self.product_dir).as_posix()


def _done(pd: Path, step: dict) -> bool:
    """A step is done when its source is gone and its destination exists. The path that is unique to the
    transaction decides: a backup's destination (backup dir) and an install's source (staging dir); an old
    file's path can be reused by a later install, so it is never the deciding side."""
    src, dst = pd / step["src"], pd / step["dst"]
    if step["kind"] == "backup":
        return dst.exists()
    return not src.exists() and dst.exists()


def _rename(pd: Path, src: str, dst: str) -> None:
    (pd / dst).parent.mkdir(parents=True, exist_ok=True)
    os.rename(pd / src, pd / dst)
    fsync_dir((pd / dst).parent)
    if (pd / src).parent != (pd / dst).parent:
        fsync_dir((pd / src).parent)


def _remove_emptied_relocation_dirs(pd: Path, doc: dict) -> None:
    for step in doc["steps"]:
        if step["kind"] == "relocate":
            parent = (pd / step["src"]).parent
            if parent != pd and parent.exists() and not any(parent.iterdir()):
                parent.rmdir()


def _verify_in_place(pd: Path, doc: dict) -> list:
    from .verify import fast_verify, verify_product

    problems = verify_product(pd, allow_journal=True)
    product = product_mod.read_product(pd)
    archived = [e["medium_id"] for e in product["media"] if e["status"] == "archived"]
    for medium in [doc["medium_id"]] if doc["medium_id"] is not None else archived:
        mdir = product_mod.medium_dir(pd, product, medium)
        problems += fast_verify(mdir, doc["holon_id"], medium, allow_journal=True)
    return [p for p in problems if p.kind != "uncommitted"]


def _forward(pd: Path, journal: Path, doc: dict, hook: Callable[[str], None]) -> None:
    for i, step in enumerate(doc["steps"]):
        hook(f"step:{i}")
        if _done(pd, step):
            continue
        _rename(pd, step["src"], step["dst"])
    _remove_emptied_relocation_dirs(pd, doc)
    hook("verify-in-place")
    problems = _verify_in_place(pd, doc)
    if problems:
        raise IntegrityFailure("new set failed verification in place (journal kept): "
                               + "; ".join(str(p) for p in problems))
    _say("verified in place")
    hook("commit")
    journal.unlink()
    fsync_dir(journal.parent)
    _say("committed")
    hook("cleanup")
    shutil.rmtree(pd / doc["backup_dir"], ignore_errors=True)
    shutil.rmtree(pd / doc["staging_dir"], ignore_errors=True)


def _can_roll_forward(pd: Path, doc: dict) -> bool:
    for step in doc["steps"]:
        src, dst = pd / step["src"], pd / step["dst"]
        if step["kind"] == "install":
            here = src if src.exists() else dst if dst.exists() else None
            if here is None or sha256_file(here) != step["sha256"]:
                return False
        elif step["kind"] == "relocate" and not (src.exists() or dst.exists()):
            return False
    return True


def _rollback(pd: Path, journal: Path, doc: dict) -> None:
    from .verify import fast_verify

    for step in reversed(doc["steps"]):
        if _done(pd, step):
            _rename(pd, step["dst"], step["src"])
    for rel in reversed(doc.get("created_dirs", [])):
        path = pd / rel
        if path.exists() and not any(p for p in path.iterdir() if p != journal):
            if journal.parent == path:
                journal.unlink()
            path.rmdir()
    problems = []
    if (pd / "product.json").exists():
        try:
            product = product_mod.read_product(pd)
            archived = [e["medium_id"] for e in product["media"] if e["status"] == "archived"]
            media = archived if doc["medium_id"] is None else [m for m in archived if m == doc["medium_id"]]
            for medium in media:
                mdir = product_mod.medium_dir(pd, product, medium)
                problems += fast_verify(mdir, doc["holon_id"], medium, allow_journal=True)
        except IntegrityFailure as exc:
            problems.append(str(exc))
    if problems:
        raise IntegrityFailure("rollback left a damaged old set (journal kept): "
                               + "; ".join(str(p) for p in problems))
    if journal.exists():
        journal.unlink()
        fsync_dir(journal.parent)
    shutil.rmtree(pd / doc["staging_dir"], ignore_errors=True)
    shutil.rmtree(pd / doc["backup_dir"], ignore_errors=True)


def journals(pd: Path) -> list[Path]:
    found = sorted(pd.glob("*.journal")) + sorted(pd.glob("*/*.journal"))
    return [p for p in found if not p.parent.name.startswith((STAGING_PREFIX, BACKUP_PREFIX))]


def recover(pd: Path, work_root: Path | None = None) -> list[str]:
    """Finish or roll back interrupted transactions; remove leftovers, including scratch dirs of killed
    reprocess/repair/staged-verification runs when work_root is given. Call with the product lock held."""
    actions = []
    if work_root is not None:
        from ..acquisition.workstate import remove_stale_scratch

        actions += [f"recovered: removed leftover scratch {name}" for name in remove_stale_scratch(work_root, pd.name)]
    if not pd.exists():
        for action in actions:
            _say(action)
        return actions
    referenced = set()
    for journal in journals(pd):
        doc = json.loads(journal.read_text(encoding="utf-8"))
        referenced |= {doc["staging_dir"], doc["backup_dir"]}
        label = f"{doc['operation']} of {doc['holon_id']}-{doc['medium_id'] or 'product'}"
        if _can_roll_forward(pd, doc):
            _forward(pd, journal, doc, lambda label: None)
            actions.append(f"recovered: rolled {label} forward")
        else:
            _rollback(pd, journal, doc)
            actions.append(f"recovered: rolled {label} back")
    for path in sorted(pd.iterdir()):
        if path.is_dir() and path.name.startswith((STAGING_PREFIX, BACKUP_PREFIX)):
            shutil.rmtree(path)
            actions.append(f"recovered: removed leftover {path.name}")
    if (pd / "product.json").exists():
        try:
            product = product_mod.read_product(pd)
        except IntegrityFailure:
            product = None
        archived_dirs = set() if product is None or product_mod.is_flat(product) else {
            e["medium_id"] for e in product["media"] if e["status"] == "archived"}
    else:
        archived_dirs = set()
    for path in sorted(pd.iterdir()):
        if path.is_dir() and not path.name.startswith(".") and path.name not in archived_dirs and not any(path.iterdir()):
            path.rmdir()
            actions.append(f"recovered: removed empty directory {path.name}/")
    for action in actions:
        _say(action)
    return actions
