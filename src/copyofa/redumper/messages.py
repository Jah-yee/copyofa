"""Classification of redumper output lines against the message catalog."""

import hashlib
import re
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from ..errors import EnvironmentFailure

CLASSES = ("blocking", "warning", "info")


@dataclass(frozen=True)
class Entry:
    pattern: re.Pattern[str]
    contains: bool
    cls: str
    code: str
    acceptable: bool
    why: str

    def matches(self, line: str) -> bool:
        return bool(
            self.pattern.search(line) if self.contains else self.pattern.fullmatch(line)
        )


@dataclass(frozen=True)
class Classified:
    record: str
    line: str
    cls: str
    code: str
    acceptable: bool
    why: str
    action: str | None = None


def _parse(text: str, source: str) -> list[Entry]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise EnvironmentFailure(f"{source}: invalid TOML: {exc}") from exc
    entries = []
    for i, item in enumerate(data.get("message", [])):
        try:
            cls = item["class"]
            if cls not in CLASSES:
                raise EnvironmentFailure(
                    f"{source}: message {i + 1}: class must be one of "
                    f"{', '.join(CLASSES)}"
                )
            entries.append(
                Entry(
                    pattern=re.compile(item["pattern"]),
                    contains=item.get("match", "fullmatch") == "contains",
                    cls=cls,
                    code=item["code"],
                    acceptable=bool(item.get("acceptable", cls != "blocking")),
                    why=item.get("why", ""),
                )
            )
        except KeyError as exc:
            raise EnvironmentFailure(
                f"{source}: message {i + 1}: missing key {exc}"
            ) from exc
        except re.error as exc:
            raise EnvironmentFailure(
                f"{source}: message {i + 1}: invalid pattern: {exc}"
            ) from exc
    return entries


class Catalog:
    def __init__(self, entries: list[Entry], user_path: Path | None = None) -> None:
        self.entries = entries
        self.user_path = user_path

    @classmethod
    def load(cls, user_path: Path | None = None) -> Catalog:
        entries: list[Entry] = []
        if user_path is not None and user_path.exists():
            entries += _parse(user_path.read_text(encoding="utf-8"), str(user_path))
        packaged = (resources.files("copyofa") / "data" / "messages.toml").read_text(
            encoding="utf-8"
        )
        entries += _parse(packaged, "packaged messages.toml")
        return cls(entries, user_path)

    def classify_line(self, record: str, line: str) -> Classified | None:
        is_message = line.startswith(("warning:", "error:"))
        for entry in self.entries:
            if (entry.contains or is_message) and entry.matches(line):
                return Classified(
                    record, line, entry.cls, entry.code, entry.acceptable, entry.why
                )
        if is_message:
            digest = hashlib.sha1(line.encode("utf-8")).hexdigest()[:8]
            where = self.user_path or "<config dir>/messages.toml"
            return Classified(
                record,
                line,
                "blocking",
                f"unclassified-{digest}",
                True,
                "unknown redumper message",
                f"classify it in {where} or accept it",
            )
        return None

    def classify(self, record: str, lines: list[str]) -> list[Classified]:
        result = []
        for line in lines:
            item = self.classify_line(record, line)
            if item is not None:
                result.append(item)
        return result
