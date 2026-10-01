import subprocess
import sys
from pathlib import Path

import pytest

import txn_scenarios as sc
from copyofa.archive.transaction import recover
from copyofa.archive.verify import verify_product


class Crash(Exception):
    pass


def labels_and_maps(
    kind: str, tmp_path: Path
) -> tuple[list[str], dict[str, str], dict[str, str]]:
    seen: list[str] = []
    root = tmp_path / "reference"
    pd = sc.fresh(kind, root)
    old = sc.file_map(pd)
    sc.act(kind, root, pd, seen.append)
    new = sc.file_map(pd)
    assert old != new
    assert verify_product(pd) == []
    return seen, old, new


def assert_clean(
    pd: Path, old: dict[str, str], new: dict[str, str], label: str
) -> None:
    current = sc.file_map(pd)
    assert current in (old, new), f"mixed state after crash at {label}"
    leftovers = [
        p.name
        for p in pd.rglob("*")
        if p.name.endswith(".journal") or p.name.startswith(".coa-")
    ]
    assert leftovers == [], label
    if current:
        assert verify_product(pd) == [], label


@pytest.mark.parametrize("kind", sc.KINDS)
def test_crash_at_every_step_recovers_old_or_new(kind: str, tmp_path: Path) -> None:
    labels, old, new = labels_and_maps(kind, tmp_path)
    assert labels[0] == "staged"
    assert labels[-1] == "cleanup"
    assert any(label.startswith("step:") for label in labels)
    for label in labels:
        root = tmp_path / f"crash-{label.replace(':', '-')}"
        pd = sc.fresh(kind, root)

        def hook(current: str, label: str = label) -> None:
            if current == label:
                raise Crash(label)

        with pytest.raises(Crash):
            sc.act(kind, root, pd, hook)
        recover(pd)
        assert_clean(pd, old, new, label)


@pytest.mark.parametrize("kind", sc.KINDS)
def test_process_death_at_every_step_recovers_old_or_new(
    kind: str, tmp_path: Path
) -> None:
    labels, old, new = labels_and_maps(kind, tmp_path)
    child = Path(__file__).parent / "txn_child.py"
    for label in labels:
        root = tmp_path / f"kill-{label.replace(':', '-')}"
        pd = sc.fresh(kind, root)
        proc = subprocess.run(
            [sys.executable, str(child), kind, str(root), label],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 137, proc.stderr
        recover(pd)
        assert_clean(pd, old, new, label)


def test_damaged_staging_rolls_back(tmp_path: Path) -> None:
    labels, old, _new = labels_and_maps("reseal", tmp_path)
    root = tmp_path / "damaged"
    pd = sc.fresh("reseal", root)
    target = next(label for label in labels if label.startswith("step:"))

    def hook(current: str) -> None:
        if current == target:
            staging = next(pd.glob(".coa-staging-*"))
            next(staging.glob("*.scram.flac")).write_bytes(b"damaged")
            raise Crash(current)

    with pytest.raises(Crash):
        sc.act("reseal", root, pd, hook)
    actions = recover(pd)
    assert any("back" in a for a in actions)
    assert sc.file_map(pd) == old


def test_recover_removes_scratch_of_killed_locked_runs(tmp_path: Path) -> None:
    pd = sc.fresh("publish-flat", tmp_path / "root")
    scratch = tmp_path / "work" / ".tmp"
    names = [
        "reprocess-8W20X-CD1-1",
        "repair-8W20X-CD1-2",
        "staged-8W20X-CD1-3",  # only made under the lock
        "deep-8W20X-CD1-4",  # coa verify --deep runs unlocked and may be live
        "reprocess-T02PQ-CD1-5",
    ]  # another product
    for name in names:
        (scratch / name / "raw").mkdir(parents=True)
    recover(pd, tmp_path / "work")
    assert sorted(p.name for p in scratch.iterdir()) == [
        "deep-8W20X-CD1-4",
        "reprocess-T02PQ-CD1-5",
    ]
