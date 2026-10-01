"""Holon IDs, medium IDs and media lists."""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable

from .errors import UsageError

if TYPE_CHECKING:
    from .vocabulary import Vocabulary

HOLON_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_HOLON_INDEX = {c: i for i, c in enumerate(HOLON_ALPHABET)}
_MEDIUM_RE = re.compile(r"^([A-Z]+)([1-9][0-9]*)([A-Z]?)$")


def holon_check_ok(holon: str) -> bool:
    """Luhn mod 32 over the Holon alphabet: the total must be a multiple of 32."""
    total = 0
    for position, char in enumerate(reversed(holon)):
        product = _HOLON_INDEX[char] * (2 if position % 2 else 1)
        total += product // 32 + product % 32
    return total % 32 == 0


def normalize_holon_id(text: str) -> str:
    """Return the canonical Holon ID or raise UsageError naming the failed rule."""
    canonical = (
        text.upper().replace(" ", "").replace("-", "").replace("I", "1").replace("L", "1").replace("O", "0")
    )
    bad = sorted({c for c in canonical if c not in _HOLON_INDEX})
    if bad:
        raise UsageError(
            f"invalid Holon ID {text!r}: characters {''.join(bad)!r} are not in the alphabet {HOLON_ALPHABET}"
        )
    if len(canonical) != 5:
        raise UsageError(f"invalid Holon ID {text!r}: must have 5 characters, got {len(canonical)}")
    if not holon_check_ok(canonical):
        raise UsageError(f"invalid Holon ID {text!r}: check character does not match (Luhn mod 32)")
    return canonical


@dataclass(frozen=True, order=False)
class MediumId:
    prefix: str
    number: int
    side: str = ""

    def __str__(self) -> str:
        return f"{self.prefix}{self.number}{self.side}"


def parse_medium_id(text: str) -> MediumId:
    match = _MEDIUM_RE.match(text.strip().upper())
    if not match:
        raise UsageError(
            f"invalid medium ID {text!r}: medium IDs are <prefix><number>[side], "
            "numbers start at 1 and are never zero-padded"
        )
    return MediumId(match.group(1), int(match.group(2)), match.group(3))


def validate_media_list(value: "str | Iterable[str]", vocab: "Vocabulary") -> list[MediumId]:
    """Parse and validate a declared media list (order preserved)."""
    items = re.split(r"[,\s]+", value.strip()) if isinstance(value, str) else [str(v) for v in value]
    items = [item for item in items if item]
    if not items:
        raise UsageError("the media list is empty")
    media = [parse_medium_id(item) for item in items]
    seen: set[str] = set()
    for medium in media:
        key = str(medium)
        if key in seen:
            raise UsageError(f"medium {key} appears twice in the media list")
        seen.add(key)
        prefix = vocab.prefixes.get(medium.prefix)
        if prefix is None:
            raise UsageError(
                f"unknown medium prefix {medium.prefix!r} in {key}; known prefixes: {', '.join(vocab.prefixes)} "
                f"(vocabulary {vocab.path})"
            )
        if medium.side:
            if prefix.sides == "none":
                raise UsageError(f"{key}: prefix {medium.prefix} does not take a side letter")
            if medium.side not in ("A", "B"):
                raise UsageError(f"{key}: side letter must be A or B")
        elif prefix.sides == "required":
            raise UsageError(f"{key}: prefix {medium.prefix} requires a side letter (A or B)")
    sided = {(m.prefix, m.number) for m in media if m.side}
    unsided = {(m.prefix, m.number) for m in media if not m.side}
    for prefix_name, number in sorted(sided):
        if (prefix_name, number) in unsided:
            raise UsageError(f"{prefix_name}{number} is listed both with and without a side letter")
        for side in ("A", "B"):
            if f"{prefix_name}{number}{side}" not in seen:
                raise UsageError(
                    f"two-sided medium {prefix_name}{number} must list both sides: "
                    f"{prefix_name}{number}A and {prefix_name}{number}B"
                )
    return media
