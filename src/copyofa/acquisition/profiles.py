"""Acquisition profiles."""

import tomllib
from dataclasses import dataclass
from importlib import resources

ORDER = ("cd-audio-pressed", "cd-provisional")


@dataclass(frozen=True)
class Stage:
    command: str
    label: str | None
    options: tuple[str, ...]
    when: str | None

    def as_dict(self) -> dict:
        return {"command": self.command, "label": self.label, "options": list(self.options), "when": self.when}


@dataclass(frozen=True)
class Profile:
    name: str
    version: str
    status: str
    family: str
    description: str
    strict_q: bool  # bad Q at track/index changes and uncertain MCN/ISRC need an explicit acceptance
    match: dict
    stages: tuple[Stage, ...]

    @property
    def key(self) -> str:
        return f"{self.name}@{self.version}"

    def approved(self, approved_list) -> bool:
        return self.key in approved_list and self.status != "provisional-only"

    def matches(self, medium, prefix: str) -> bool:
        if self.family != medium.family:
            return False
        rules = {
            "prefixes": lambda v: prefix in v,
            "media_types": lambda v: medium.media_type in v,
            "sessions": lambda v: medium.sessions == v,
            "data_tracks": lambda v: medium.data_tracks == v,
            "disc_status": lambda v: medium.disc_status == v,
        }
        return all(rules[key](value) for key, value in self.match.items())


def load(name: str) -> Profile:
    text = (resources.files("copyofa") / "data" / "profiles" / f"{name}.toml").read_text(encoding="utf-8")
    data = tomllib.loads(text)
    return Profile(
        name=data["name"],
        version=data["version"],
        status=data["status"],
        family=data["family"],
        description=data.get("description", ""),
        strict_q=bool(data.get("strict_q", False)),
        match=dict(data.get("match", {})),
        stages=tuple(
            Stage(s["command"], s.get("label"), tuple(s.get("options", [])), s.get("when"))
            for s in data.get("stages", [])
        ),
    )


def match_profile(medium, prefix: str) -> Profile | None:
    for name in ORDER:
        profile = load(name)
        if profile.matches(medium, prefix):
            return profile
    return None
