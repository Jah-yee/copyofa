import pytest

from copyofa.redumper.messages import Catalog

LITERALS = {
    "warning: drive not found in the database": ("blocking", "drive-not-in-database", False),
    "warning: unsupported drive firmware": ("blocking", "unsupported-firmware", False),
    "warning: drive read offset not found in the database": ("blocking", "drive-offset-unknown", False),
    "warning: using generic drive": ("blocking", "generic-drive", False),
    "warning: drive doesn't support reading of subchannel data": ("blocking", "no-subchannel", False),
    "warning: drive doesn't support C2 error pointers": ("blocking", "no-c2", False),
    "[LBA:  -1234] SCSI error (HOST: SHORT TRANSFER)": ("blocking", "scsi-short-transfer", False),
    "warning: TOC / QTOC mismatch, track 3 index 1 position": ("blocking", "toc-qtoc-mismatch", True),
    "warning: split is performed by QTOC": ("blocking", "toc-qtoc-mismatch", True),
    "warning: offset shift detected, to apply correction please use an option": ("blocking", "offset-shift", True),
    "error: data errors detected, unable to continue": ("blocking", "split-refused", True),
    "warning: removed postscribed ID (track: 2)": ("blocking", "data-modified", True),
    "warning: descramble failed (LBA: 1234)": ("blocking", "descramble-failed", True),
    "warning: descramble failed (LBA: [1 .. 5])": ("blocking", "descramble-failed", True),
    "warning: incomplete pre-gap (session: 1, unavailable: 10/150)": ("blocking", "incomplete-pregap", True),
    "warning: incomplete pre-gap (session: 2, unavailable: 75/150)": ("info", "intersession-pregap", True),
    "warning: CD-R lead-in/lead-out C2 errors detected": ("warning", "cdr-c2-tolerated", True),
    "warning: CD-R trailing C2 errors detected (sectors: 3)": ("warning", "cdr-c2-tolerated", True),
    "warning: non-zero data range is not continuous, skipping Universal Hash calculation": (
        "info", "universal-hash-skipped", True,
    ),
}


@pytest.mark.parametrize("line,expected", LITERALS.items())
def test_packaged_patterns(line, expected):
    item = Catalog.load().classify_line("r", line)
    assert (item.cls, item.code, item.acceptable) == expected


def test_unknown_lines_block_and_codes_are_stable():
    catalog = Catalog.load()
    first = catalog.classify_line("r", "warning: brand new")
    assert first.cls == "blocking" and first.code.startswith("unclassified-") and len(first.code) == 21
    assert Catalog.load().classify_line("r", "warning: brand new").code == first.code
    assert catalog.classify_line("r", "plain progress line") is None


def test_user_catalog_overrides(tmp_path):
    user = tmp_path / "messages.toml"
    user.write_text(
        "[[message]]\npattern = 'warning: brand new'\nclass = \"info\"\ncode = \"brand-new\"\nwhy = \"tested\"\n"
    )
    assert Catalog.load(user).classify_line("r", "warning: brand new").code == "brand-new"
