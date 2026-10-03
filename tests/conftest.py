import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
PILOT_ROOT = Path(os.environ.get("COPYOFA_PILOT_DUMPS", str(Path.home() / "Dumps")))


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


def pilot_dir(name: str) -> Path:
    """Directory of a pilot dump; skips the test when it is absent."""
    path = PILOT_ROOT / name
    if not path.is_dir():
        pytest.skip(f"pilot dump {path} not available")
    return path


def pilot_file(name: str, ext: str) -> Path:
    directory = pilot_dir(name)
    stem = "T02PQ" if name.startswith("Broken-") else name
    path = directory / f"{stem}.{ext}"
    if not path.exists():
        pytest.skip(f"pilot file {path} not available")
    return path
