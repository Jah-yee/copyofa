"""Shared medium-prefix vocabulary (`<archive_root>/media-prefixes.toml`)."""

import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .errors import EnvironmentFailure

FILE_NAME = "media-prefixes.toml"


@dataclass(frozen=True)
class Prefix:
    name: str
    media: tuple[str, ...]
    handled_by: str
    partial: bool
    sides: str
    partial_reason: str | None = None


@dataclass(frozen=True)
class Vocabulary:
    path: Path
    prefixes: dict[str, Prefix]


def default_text() -> str:
    return (resources.files("copyofa") / "data" / "media-prefixes.default.toml").read_text(encoding="utf-8")


def parse(text: str, path: Path) -> Vocabulary:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise EnvironmentFailure(f"{path}: invalid TOML: {exc}") from exc
    prefixes: dict[str, Prefix] = {}
    for name, entry in data.get("prefixes", {}).items():
        sides = entry.get("sides", "none")
        if sides not in ("none", "optional", "required"):
            raise EnvironmentFailure(f"{path}: prefix {name}: sides must be none, optional or required")
        prefixes[name] = Prefix(
            name=name,
            media=tuple(entry.get("media", [])),
            handled_by=entry.get("handled_by", "coa"),
            partial=bool(entry.get("partial", False)),
            sides=sides,
            partial_reason=entry.get("partial_reason"),
        )
    return Vocabulary(path=path, prefixes=prefixes)


def load(archive_root: Path) -> Vocabulary:
    path = archive_root / FILE_NAME
    if not path.exists():
        raise EnvironmentFailure(f"missing prefix vocabulary {path}: run coa init")
    return parse(path.read_text(encoding="utf-8"), path)
