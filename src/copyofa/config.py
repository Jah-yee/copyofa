"""Configuration file (`~/.config/copyofa/config.toml`) and `copyofa init`."""

import json
import os
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from string import Template
from typing import Any

from . import vocabulary
from .errors import EnvironmentFailure, UsageError
from .redumper.commands import MIN_BUILD

DEFAULT_PATH = Path("~/.config/copyofa/config.toml")
PRIVATE_FOLDERS = ("Desktop", "Documents", "Downloads")


@dataclass(frozen=True)
class DriveConfig:
    name: str
    vendor: str
    product: str
    firmware: str | None  # None accepts any firmware
    media: tuple[str, ...]


@dataclass(frozen=True)
class KnownBad:
    bridge: str
    fixed_by: tuple[str, ...]  # build patch suffixes that fix it
    fixed_in: int | None  # first upstream build that fixes it
    reason: str


@dataclass(frozen=True)
class Config:
    path: Path
    archive_root: Path
    work_root: Path
    redumper_path: str
    min_build: int
    drives: tuple[DriveConfig, ...]
    known_bad: tuple[KnownBad, ...]
    approved_profiles: tuple[str, ...]
    redundancy_percent: int
    q_rate_threshold: float
    min_free_work_bytes: int
    min_free_archive_bytes: int
    raw: dict[str, Any] = field(compare=False, repr=False)

    @property
    def config_dir(self) -> Path:
        return self.path.parent

    @property
    def user_messages_path(self) -> Path:
        return self.config_dir / "messages.toml"

    def drive(self, name: str) -> DriveConfig:
        for drive in self.drives:
            if drive.name == name:
                return drive
        raise UsageError(
            f"unknown drive {name!r}; configured drives: "
            f"{', '.join(d.name for d in self.drives)}"
        )

    def as_metadata(self) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(json.dumps(self.raw))
        return result


def resolve_path(cli_path: str | None) -> Path:
    if cli_path:
        return Path(cli_path).expanduser()
    env = os.environ.get("COPYOFA_CONFIG")
    if env:
        return Path(env).expanduser()
    return DEFAULT_PATH.expanduser()


def check_private_folder(path: Path) -> None:
    """The work root must stay outside macOS privacy-controlled folders."""
    home = Path.home().resolve()
    resolved = path.expanduser().resolve()
    for folder in PRIVATE_FOLDERS:
        if resolved.is_relative_to(home / folder):
            raise EnvironmentFailure(
                f"work root {resolved} is inside ~/{folder}, "
                "a macOS privacy-controlled folder; choose another work_root",
                code="work-root-private",
            )


def load(cli_path: str | None = None) -> Config:
    path = resolve_path(cli_path)
    if not path.exists():
        raise EnvironmentFailure(
            f"no config at {path}: run copyofa init --archive-root PATH"
            " --work-root PATH",
            code="no-config",
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise EnvironmentFailure(
            f"{path}: invalid TOML: {exc}", code="bad-config"
        ) from exc
    for key in ("archive_root", "work_root"):
        if not data.get(key):
            raise EnvironmentFailure(
                f"no config value {key} in {path}: run copyofa init or add it",
                code="no-config",
            )
    work_root = Path(data["work_root"]).expanduser()
    check_private_folder(work_root)
    red = data.get("redumper", {})
    min_build = int(red.get("min_build", MIN_BUILD))
    if min_build < MIN_BUILD:
        raise EnvironmentFailure(
            f"{path}: [redumper] min_build = {min_build} is below b{MIN_BUILD}, the "
            f"oldest build copyofa supports",
            code="bad-config",
        )
    redundancy = data.get("redundancy", {})
    space = data.get("space", {})
    try:
        drives = tuple(
            DriveConfig(
                name=d["name"],
                vendor=d["vendor"],
                product=d["product"],
                firmware=d.get("firmware"),
                media=tuple(d.get("media", [])),
            )
            for d in data.get("drives", [])
        )
        known_bad = tuple(
            KnownBad(
                bridge=k["bridge"],
                fixed_by=tuple(k.get("fixed_by", [])),
                fixed_in=int(k["fixed_in"]) if "fixed_in" in k else None,
                reason=k.get("reason", ""),
            )
            for k in data.get("known_bad", [])
        )
    except KeyError as exc:
        raise EnvironmentFailure(
            f"{path}: missing key {exc} in a [[drives]] or [[known_bad]] entry"
        ) from exc
    return Config(
        path=path,
        archive_root=Path(data["archive_root"]).expanduser(),
        work_root=work_root,
        redumper_path=red.get("path", "redumper"),
        min_build=min_build,
        drives=drives,
        known_bad=known_bad,
        approved_profiles=tuple(data.get("profiles", {}).get("approved", [])),
        redundancy_percent=int(redundancy.get("percent", 10)),
        q_rate_threshold=float(redundancy.get("q_rate_threshold", 0.005)),
        min_free_work_bytes=int(space.get("min_free_work_bytes", 3_000_000_000)),
        min_free_archive_bytes=int(space.get("min_free_archive_bytes", 1_500_000_000)),
        raw=data,
    )


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def init(config_path: Path, archive_root: Path, work_root: Path) -> list[Path]:
    """Write the config template and create the roots; return the paths
    written or used."""
    if config_path.exists():
        raise UsageError(f"config {config_path} already exists; edit it instead")
    archive_root = archive_root.expanduser().resolve()
    work_root = work_root.expanduser().resolve()
    check_private_folder(work_root)
    template = (resources.files("copyofa") / "data" / "config.template.toml").read_text(
        encoding="utf-8"
    )
    text = Template(template).substitute(
        archive_root=_toml_string(str(archive_root)),
        work_root=_toml_string(str(work_root)),
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    archive_root.mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)
    config_path.write_text(text, encoding="utf-8")
    prefixes = archive_root / vocabulary.FILE_NAME
    if not prefixes.exists():
        prefixes.write_text(vocabulary.default_text(), encoding="utf-8")
    return [config_path, archive_root, work_root, prefixes]
