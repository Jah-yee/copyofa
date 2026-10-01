"""Medium metadata document and schema validation."""

import json
import platform
import sys
from functools import cache
from importlib import resources
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from .. import __version__
from ..config import Config
from ..errors import IntegrityFailure
from ..util import utc_now
from . import encoding

if TYPE_CHECKING:
    from ..acquisition.acquire import Run

FORMAT_VERSION = "0.1"
SCHEMA_VERSION = "0.1"
PRE_DUMP_DECISIONS = ("provisional-capture", "premounted-read-write")


@cache
def schema(kind: str) -> dict[str, Any]:
    text = (
        resources.files("copyofa") / "data" / "schemas" / f"{kind}-0.1.schema.json"
    ).read_text(encoding="utf-8")
    doc: dict[str, Any] = json.loads(text)
    return doc


def keys_of_class(cls: str) -> list[str]:
    return [
        k for k, v in schema("medium")["properties"].items() if v["x-coa-class"] == cls
    ]


def validate(doc: dict[str, Any], kind: str) -> None:
    errors = sorted(
        Draft202012Validator(schema(kind)).iter_errors(doc), key=lambda e: list(e.path)
    )
    if errors:
        details = "; ".join(
            f"/{'/'.join(str(p) for p in e.path)}: {e.message}" for e in errors[:5]
        )
        raise IntegrityFailure(f"{kind} metadata does not match its schema: {details}")


def tool_info(cfg: Config, build: str | None) -> dict[str, Any]:
    versions = encoding.tool_versions()
    return {
        "name": "coa",
        "version": __version__,
        "host_os": {
            "name": "macOS",
            "version": platform.mac_ver()[0],
            "machine": platform.machine(),
        },
        "python": sys.version.split()[0],
        "dependencies": {"redumper": build, **versions},
        "config": cfg.as_metadata(),
    }


def redundancy_doc(
    cfg: Config,
    state_redundancy: dict[str, Any],
    holon: str,
    medium: str,
    payload: list[str],
) -> dict[str, Any]:
    import shlex

    from .archiveset import par2_argv

    override = state_redundancy.get("override")
    enabled = (
        override["value"] if override else bool(state_redundancy.get("default_enabled"))
    )
    percent = cfg.redundancy_percent if enabled else None
    par2 = None
    if enabled:
        par2 = {
            "implementation": "par2cmdline",
            "version": encoding.tool_versions()["par2"],
            "command": shlex.join(
                par2_argv(holon, medium, payload, percent)  # type: ignore[arg-type]
            ),
        }
    return {
        "enabled": enabled,
        "percent": percent,
        "default_enabled": state_redundancy.get("default_enabled"),
        "default_reason": state_redundancy.get("default_reason"),
        "override": override,
        "par2": par2,
    }


def build_medium(
    *,
    cfg: Config,
    run: Run,
    derived: dict[str, Any],
    media_list: list[str],
    outcome: str,
    gaps: list[dict[str, Any]],
    stages: list[dict[str, Any]],
    acquisition_errors: dict[str, Any],
) -> dict[str, Any]:
    state = run.state
    holon, medium = run.holon, str(run.medium)
    profile = state["profile"]
    provisional_needed = any(
        i["code"] == "provisional-capture" for i in state["pre_dump_issues"]
    )
    permanent = profile["approved"] and not provisional_needed
    reason = (
        f"captured with approved profile {profile['name']} {profile['version']}"
        if permanent
        else f"profile {profile['name']} {profile['version']} is "
        f"{profile['status']}, not approved"
    )
    prefix = run.vocab.prefixes[run.medium.prefix]
    payload = [f["stored_as"] for f in derived["raw_files"]]
    doc: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "holon_id": holon,
        "medium_id": medium,
        "position": media_list.index(medium) + 1,
        "media_list": list(media_list),
        "partial": {"prefix": prefix.name, "reason": prefix.partial_reason}
        if prefix.partial
        else None,
        "created_at": utc_now(),
        "outcome": outcome,
        "tool": tool_info(cfg, state["environment"]["build"]),
        "profile": {
            "name": profile["name"],
            "version": profile["version"],
            "status": profile["status"],
            "approved": profile["approved"],
            "permanence": "permanent" if permanent else "provisional",
            "reason": reason,
            "stages": profile["stages"],
        },
        "drive": derived["drive"],
        "medium": derived["medium"],
        "stages": stages,
        "resumes": state["resumes"],
        "acquisition_errors": acquisition_errors,
    }
    for key in (
        "remaining_errors",
        "lba_start",
        "write_offset_samples",
        "write_offset",
        "nonzero_sample_ranges",
        "universal_hash_sha1",
        "mcn",
        "cd_text",
        "sessions",
        "tracks",
        "raw_files",
    ):
        doc[key] = derived[key]
    doc["redundancy"] = redundancy_doc(cfg, state["redundancy"], holon, medium, payload)
    doc["gaps"] = gaps
    doc["accepted_issues"] = state["acceptances"]
    doc["issues"] = derived["issues"]
    doc["log_messages"] = derived["log_messages"]
    doc["checks"] = derived["checks"]
    doc["external_verification"] = {
        "redump": {"result": "not checked"},
        "accuraterip": {"result": "not checked"},
        "ctdb": {"result": "not checked"},
    }
    doc["revisions"] = []
    validate(doc, "medium")
    return doc


def recorded_inputs(doc: dict[str, Any]) -> dict[str, Any]:
    """The recorded fields derive() needs, taken from an existing metadata document."""
    from ..acquisition import profiles

    try:
        strict_q = profiles.load(doc["profile"]["name"]).strict_q
    except FileNotFoundError:
        strict_q = False
    return {
        "stages": doc["stages"],
        "accepted_issues": doc["accepted_issues"],
        "profile": doc["profile"] | {"strict_q": strict_q},
        "checks": [c for c in doc["checks"] if c["origin"] == "acquisition"],
        "acquisition_errors": doc["acquisition_errors"],
    }
