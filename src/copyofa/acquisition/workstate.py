"""Per-medium work directory and its internal state file (resumable acquisition)."""

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..util import atomic_write_json, sha256_file, utc_now

STATE_VERSION = 1

RECORD_STAGE_NAMES = {
    "dump": "dump",
    "dump::extra": "dump-extra",
    "protection": "protection",
    "refine": "refine",
    "split": "split",
    "hash": "hash",
    "info": "info",
}


@dataclass(frozen=True)
class WorkPaths:
    work_root: Path
    holon: str
    medium: str

    @property
    def image(self) -> str:
        return f"{self.holon}-{self.medium}"

    @property
    def root(self) -> Path:
        return self.work_root / self.image

    @property
    def dump(self) -> Path:
        return self.root / "dump"

    @property
    def records(self) -> Path:
        return self.root / "records"

    @property
    def state_file(self) -> Path:
        return self.root / "state.json"

    @property
    def lock(self) -> Path:
        return self.root / "work.lock"

    def verify_dir(self, nn: int) -> Path:
        return self.root / f"verify-{nn:02d}"

    def stage_record(self, nn: int, command: str) -> Path:
        return (
            self.records
            / f"{self.image}.stage-{nn:02d}-{RECORD_STAGE_NAMES[command]}.console"
        )

    def verify_log(self, nn: int) -> Path:
        return self.records / f"{self.image}.verify-{nn:02d}.log"

    def preflight(self, name: str) -> Path:
        return self.records / f"{self.image}.preflight-{name}"


def drive_lock_path(work_root: Path, vendor: str, product: str, firmware: str) -> Path:
    product_key = product.replace(" ", "_")
    return work_root / ".locks" / f"drive-{vendor}_{product_key}_{firmware}.lock"


def drive_test_key(
    vendor: str,
    product: str,
    firmware: str,
    interconnect: str,
    bridge: str | None,
    build: str,
) -> str:
    text = f"{vendor}|{product}|{firmware}|{interconnect}|{bridge or 'none'}|{build}"
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def drive_tests_dir(work_root: Path) -> Path:
    return work_root / ".drive-tests"


def product_marker(work_root: Path, holon: str) -> Path:
    return work_root / ".products" / f"{holon}.json"


def scratch_root(work_root: Path) -> Path:
    path = work_root / ".tmp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_state(holon: str, medium: str) -> dict[str, Any]:
    return {
        "state_version": STATE_VERSION,
        "holon_id": holon,
        "medium_id": medium,
        "declared_media_list": None,
        "created_at": utc_now(),
        "environment": None,
        "medium": None,
        "prefix": None,
        "profile": None,
        "pre_dump_issues": [],
        "gaps": [],
        "stages": [],
        "plan_index": 0,
        "skipped_stages": [],
        "resumes": [],
        "first_pass": None,
        "leadout_lba": None,
        "redundancy": {
            "default_enabled": None,
            "default_reason": None,
            "override": None,
        },
        "raw_snapshot": {},
        "acquisition_complete": False,
        "verification_needed": False,
        "force_split": False,
        "verifications": [],
        "issues": [],
        "acceptances": [],
        "acquisition_checks": [],
        "log_messages": [],
        "phase": "identifying",
    }


def load_state(paths: WorkPaths) -> dict[str, Any] | None:
    if not paths.state_file.exists():
        return None
    state: dict[str, Any] = json.loads(paths.state_file.read_text(encoding="utf-8"))
    if state.get("state_version") != STATE_VERSION:
        raise RuntimeError(
            f"{paths.state_file}: unsupported state version "
            f"{state.get('state_version')}"
        )
    return state


def save_state(paths: WorkPaths, state: dict[str, Any]) -> None:
    atomic_write_json(paths.state_file, state)


def next_nn(state: dict[str, Any]) -> int:
    return 1 + max(
        (s["nn"] for s in state["stages"] if s.get("nn") is not None), default=0
    )


def snapshot(directory: Path) -> dict[str, Any]:
    return {
        path.name: {"size": path.stat().st_size, "sha256": sha256_file(path)}
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def set_read_only(directory: Path, read_only: bool) -> None:
    for path in directory.iterdir():
        if path.is_file() and not path.is_symlink():
            path.chmod(0o444 if read_only else 0o644)


def make_tree_writable(root: Path) -> None:
    for dirpath, _dirnames, filenames in os.walk(root):
        Path(dirpath).chmod(0o755)
        for name in filenames:
            path = Path(dirpath) / name
            if not path.is_symlink():
                path.chmod(path.stat().st_mode | stat.S_IWUSR)


LOCKED_SCRATCH_PREFIXES = ("reprocess", "repair", "staged")


def remove_stale_scratch(work_root: Path, holon: str) -> list[str]:
    """Delete scratch dirs left by killed reprocess/repair/staged-verification runs
    of a product.

    These are only created while holding the product lock, so with the lock held
    none of them is live. (`copyofa verify --deep` runs without the lock and uses a
    different prefix; its dirs are never touched here.)
    """
    import shutil

    root = work_root / ".tmp"
    removed = []
    if root.is_dir():
        for prefix in LOCKED_SCRATCH_PREFIXES:
            for path in sorted(root.glob(f"{prefix}-{holon}-*")):
                shutil.rmtree(path, ignore_errors=True)
                removed.append(path.name)
    return removed
