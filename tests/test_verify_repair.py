import json

import pytest
import txn_scenarios as sc

from copyofa import config
from copyofa.archive import product
from copyofa.archive.transaction import checksum_only_verifier
from copyofa.archive.verify import fast_verify, repair_medium, verify_command
from copyofa.errors import EXIT_INTEGRITY, IntegrityFailure

H = sc.H


@pytest.fixture
def archived(tmp_path):
    cfg_path = tmp_path / "config.toml"
    config.init(cfg_path, tmp_path / "archive", tmp_path / "work")
    cfg = config.load(str(cfg_path))
    pd = cfg.archive_root / H
    pd.mkdir()
    raw = sc.synthetic.raw_files(tmp_path / "raw", f"{H}-CD1", seed=3)
    doc = sc.synthetic.metadata(H, "CD1", raw, redundancy=True)
    new = product.with_archived(product.new_product(H, ["CD1"], sc.VOCAB), "CD1", doc)

    def build(staging):
        sc.archiveset.build_set(staging, metadata=doc, raw=raw)
        product.write_product_files(staging, new)

    sc.Transaction(pd, "publish", H, "CD1", staged_verifier=checksum_only_verifier).run(build)
    return cfg, pd


def flip(path):
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))


def test_repair_restores_payload_and_records_revision(archived):
    cfg, pd = archived
    target = pd / f"{H}-CD1.subcode.xz"
    original = target.read_bytes()
    flip(target)
    problems = fast_verify(pd, H, "CD1")
    assert [p.kind for p in problems] == ["damage"] and target.name in problems[0].detail
    repaired = repair_medium(cfg, pd, product.read_product(pd), H, "CD1", staged_verifier=checksum_only_verifier)
    assert repaired == [target.name]
    assert target.read_bytes() == original
    assert fast_verify(pd, H, "CD1") == []
    revision = json.loads((pd / f"{H}-CD1.json").read_text())["revisions"][-1]
    assert revision["operation"] == "repair" and target.name in revision["reason"]
    assert not list(pd.glob("*.1"))


def test_unrecoverable_without_par2(archived, capsys):
    cfg, pd = archived
    flip(pd / f"{H}-CD1.subcode.xz")
    for path in pd.glob("*.par2"):
        path.unlink()
    with pytest.raises(IntegrityFailure, match="unrecoverable"):
        repair_medium(cfg, pd, product.read_product(pd), H, "CD1", staged_verifier=checksum_only_verifier)
    assert verify_command(cfg, H, repair=True) == EXIT_INTEGRITY
    assert "unrecoverable" in capsys.readouterr().out
