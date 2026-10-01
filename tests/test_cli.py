"""`coa` exit codes, error reporting, OUTCOME line and discard
confirmation through the Click entry point."""

from pathlib import Path
from typing import Any, NoReturn

import pytest
from click.testing import CliRunner

from copyofa import config
from copyofa.acquisition import acquire
from copyofa.acquisition.workstate import WorkPaths
from copyofa.cli import main

H = "8W20X"


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    return config.init(
        tmp_path / "config.toml", tmp_path / "archive", tmp_path / "work"
    )[0]


def test_coa_error_reports_exit_code_and_outcome(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        main, ["--config", str(tmp_path / "missing.toml"), "archive", H, "CD1"]
    )
    assert result.exit_code == 22
    assert result.stderr.startswith(
        "coa: EnvironmentFailure [no-config]: no config at "
    )
    assert result.stdout == "OUTCOME: FAIL\n"


def test_interrupt_exits_130_with_outcome(
    cfg_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*args: Any, **kwargs: Any) -> NoReturn:
        raise KeyboardInterrupt

    monkeypatch.setattr(acquire, "refine", interrupted)
    result = CliRunner().invoke(
        main, ["--config", str(cfg_path), "refine", H, "CD1", "--retries", "1"]
    )
    assert result.exit_code == 130
    assert result.stderr == "coa: interrupted\n"
    assert result.stdout == "OUTCOME: FAIL\n"


def test_usage_error_has_no_outcome() -> None:
    result = CliRunner().invoke(main, ["archive", H])
    assert result.exit_code == 2
    assert "OUTCOME" not in result.output


def test_discard_needs_yes_off_terminal(cfg_path: Path) -> None:
    root = WorkPaths(config.load(str(cfg_path)).work_root, H, "CD1").root
    root.mkdir(parents=True)
    result = CliRunner().invoke(main, ["--config", str(cfg_path), "discard", H, "CD1"])
    assert result.exit_code == 2
    assert "needs --yes" in result.stderr
    assert root.exists()
    result = CliRunner().invoke(
        main, ["--config", str(cfg_path), "discard", H, "CD1", "--yes"]
    )
    assert result.exit_code == 0
    assert not root.exists()
    assert "discarded" in result.stdout
