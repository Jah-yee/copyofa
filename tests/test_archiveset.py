import subprocess

import pytest
import synthetic

from copyofa.archive import archiveset, metadata
from copyofa.errors import IntegrityFailure

REBUILD = r"""#!/bin/bash
set -euo pipefail
shopt -s nullglob
cd "$1"
shasum -a 256 -c {P}.sha256
for f in {P}.*.flac; do flac -d --force-raw-format --endian=little --sign=signed -o "${f%.flac}" "$f"; done
for f in {P}.*.xz;   do xz -dk "$f"; done
for f in {P}.*.zst;  do zstd -d --long=31 "$f"; done
jq -r '.raw_files[] | "\(.sha256)  \(.name)"' {P}.json | shasum -a 256 -c -
"""


def test_build_set_is_verifiable_with_standard_tools(tmp_path):
    raw = synthetic.raw_files(tmp_path / "raw", "8W20X-CD1")
    doc = synthetic.metadata("8W20X", "CD1", raw)
    staging = tmp_path / "set"
    names = archiveset.build_set(staging, metadata=doc, raw=raw)
    assert "8W20X-CD1.scram.flac" in names and "8W20X-CD1.state.zst" in names and "8W20X-CD1.log.xz" in names
    assert "8W20X-CD1.par2" in names and "8W20X-CD1.sha256" in names
    assert subprocess.run(["shasum", "-a", "256", "-c", "8W20X-CD1.sha256"], cwd=staging, capture_output=True).returncode == 0
    assert subprocess.run(["par2", "verify", "-q", "8W20X-CD1.par2"], cwd=staging, capture_output=True).returncode == 0
    script = tmp_path / "rebuild.sh"
    script.write_text(REBUILD.replace("{P}", "8W20X-CD1"))
    result = subprocess.run(["bash", str(script), str(staging)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout


def test_schema_rejects_unknown_keys(tmp_path):
    raw = synthetic.raw_files(tmp_path / "raw", "8W20X-CD1")
    doc = synthetic.metadata("8W20X", "CD1", raw) | {"extra": 1}
    with pytest.raises(IntegrityFailure, match="extra"):
        metadata.validate(doc, "medium")


def test_schema_classifies_every_field():
    props = metadata.schema("medium")["properties"]
    assert set(metadata.keys_of_class("recorded")) | set(metadata.keys_of_class("re-derivable")) == set(props)
    assert "raw_files" in metadata.keys_of_class("re-derivable")
    assert "accepted_issues" in metadata.keys_of_class("recorded")
