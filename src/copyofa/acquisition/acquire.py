"""`copyofa archive`, `copyofa refine`, `copyofa accept`, `copyofa discard`,
`copyofa drive-test`: identification, safety checks, staged and resumable
acquisition."""

import contextlib
import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click

from .. import config as config_mod
from .. import vocabulary
from ..config import Config, DriveConfig
from ..errors import EXIT_FAIL, EnvironmentFailure, MediumRejected, UsageError
from ..ids import MediumId, normalize_holon_id, parse_medium_id, validate_media_list
from ..issues import Issue, open_blocking, render
from ..redumper import console, rawdata
from ..redumper.commands import build_argv, redumper_build
from ..redumper.messages import Catalog, Classified
from ..system import macos
from ..system.procs import StageRun, run_recorded
from ..util import atomic_write_json, utc_now
from . import profiles
from .workstate import (
    WorkPaths,
    drive_lock_path,
    drive_test_key,
    drive_tests_dir,
    load_state,
    make_tree_writable,
    new_state,
    next_nn,
    product_marker,
    save_state,
    set_read_only,
    snapshot,
)

REQUIRED_TOOLS = ("flac", "xz", "zstd", "par2", "drutil", "ioreg", "mount")


@dataclass
class Run:
    cfg: Config
    vocab: vocabulary.Vocabulary
    catalog: Catalog
    holon: str
    medium: MediumId
    paths: WorkPaths
    state: dict[str, Any]
    redumper: str = ""
    build: str = ""
    drive: DriveConfig | None = None
    location: macos.DriveLocation | None = None
    exit_stack: contextlib.ExitStack = field(default_factory=contextlib.ExitStack)

    @property
    def image(self) -> str:
        return self.paths.image

    def save(self) -> None:
        save_state(self.paths, self.state)


class Stop(Exception):
    """Stop the run with an exit code after the state has been saved and issues
    rendered."""

    def __init__(self, exit_code: int) -> None:
        super().__init__(exit_code)
        self.exit_code = exit_code


def stage_failed_issue(
    holon: str, medium: str, stage: str, nn: int, exit_code: int, interrupted: bool
) -> Issue:
    reason = "was interrupted" if interrupted else f"exited with status {exit_code}"
    action = (
        f"rerun: copyofa archive {holon} {medium} (repeats the stage; keep the disc "
        f"in the drive)"
    )
    return Issue(
        code="stage-failed",
        severity="blocking",
        title="redumper stage did not complete",
        what=f"stage {nn:02d} ({stage}) {reason}",
        why="the stage is incomplete; its outputs cannot be trusted until it is "
        "repeated",
        actions=[action],
        acceptable=False,
        values={
            "stage": stage,
            "nn": nn,
            "exit_code": exit_code,
            "interrupted": interrupted,
        },
    )


def catalog_issue(item: Classified, holon: str, medium: str) -> Issue:
    actions = [item.action] if item.action else []
    if item.acceptable:
        actions.append(f"copyofa accept {holon} {medium} {item.code} --reason TEXT")
    else:
        actions.append(
            f"fix the drive or environment, then copyofa discard {holon} {medium} "
            f"--yes and archive again"
        )
    return Issue(
        code=item.code,
        severity=item.cls,
        title=f"redumper message {item.code}",
        what=f"{item.record}: {item.line}",
        why=item.why,
        actions=actions,
        acceptable=item.acceptable,
        values={"record": item.record, "line": item.line},
    )


# --- setup ----------------------------------------------------------------------------


def open_run(cfg: Config, holon_text: str, medium_text: str) -> Run:
    vocab = vocabulary.load(cfg.archive_root)
    holon = normalize_holon_id(holon_text)
    medium = parse_medium_id(medium_text)
    prefix = vocab.prefixes.get(medium.prefix)
    if prefix is None:
        raise UsageError(
            f"unknown medium prefix {medium.prefix!r} (vocabulary {vocab.path})"
        )
    if prefix.handled_by != "copyofa":
        raise MediumRejected(
            f"{medium} is handled elsewhere ({prefix.handled_by})",
            code="handled-elsewhere",
        )
    paths = WorkPaths(cfg.work_root, holon, str(medium))
    return Run(
        cfg, vocab, Catalog.load(cfg.user_messages_path), holon, medium, paths, {}
    )


def check_product(run: Run, media_list: str | None) -> None:
    """The medium must be on the product's media list and not archived yet."""
    if product_marker(run.cfg.work_root, run.holon).exists():
        raise EnvironmentFailure(
            f"the layout of {run.holon} changed in an amendment: run copyofa verify "
            f"{run.holon} first",
            code="verify-required",
        )
    product_dir = run.cfg.archive_root / run.holon
    if (product_dir / "product.json").exists():
        from ..archive import product as product_mod

        doc = product_mod.read_product(product_dir)
        entry = next(
            (m for m in doc["media"] if m["medium_id"] == str(run.medium)), None
        )
        if entry is None:
            raise MediumRejected(
                f"{run.medium} is not in the media list of {run.holon}: "
                f"{', '.join(m['medium_id'] for m in doc['media'])}; use copyofa amend",
                code="not-in-media-list",
            )
        if entry["status"] == "archived":
            raise MediumRejected(
                f"{run.holon} {run.medium} is already archived", code="already-archived"
            )
        if media_list:
            raise UsageError(
                f"media list already declared for {run.holon}; use copyofa amend"
            )
        return
    declared = run.state.get("declared_media_list")
    if media_list:
        parsed = [str(m) for m in validate_media_list(media_list, run.vocab)]
        if declared and parsed != declared:
            raise UsageError(
                f"media list differs from the one declared earlier: "
                f"{', '.join(declared)}"
            )
        declared = parsed
    if not declared:
        raise UsageError(
            f"{run.holon} has no product manifest yet: declare its media with "
            f"--media-list"
        )
    if str(run.medium) not in declared:
        raise MediumRejected(
            f"{run.medium} is not in the declared media list {', '.join(declared)}",
            code="not-in-media-list",
        )
    run.state["declared_media_list"] = declared


def environment_preflight(cfg: Config) -> tuple[str, str]:
    """Checks run on every invocation: tools, redumper allow-list, work root rules,
    free space."""
    redumper = shutil.which(cfg.redumper_path)
    missing = [t for t in REQUIRED_TOOLS if not shutil.which(t)]
    if redumper is None:
        missing.insert(0, cfg.redumper_path)
    if missing:
        raise EnvironmentFailure(
            f"missing tools on PATH: {', '.join(missing)}", code="missing-tool"
        )
    assert redumper is not None  # missing-tool above raises when shutil.which failed
    build = redumper_build(redumper)
    if build is None or build not in cfg.allowed_builds:
        raise EnvironmentFailure(
            f"redumper build {build!r} is not on the allow-list "
            f"{list(cfg.allowed_builds)}",
            code="redumper-not-allowed",
        )
    config_mod.check_private_folder(cfg.work_root)
    cfg.work_root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(cfg.work_root).free
    if free < cfg.min_free_work_bytes:
        raise EnvironmentFailure(
            f"only {free} bytes free on the work volume, "
            f"{cfg.min_free_work_bytes} required",
            code="low-disk-space",
        )
    return redumper, build


# --- drive selection --------------------------------------------------------------


@dataclass
class Selected:
    drive: DriveConfig
    location: macos.DriveLocation
    status: macos.MediumStatus
    interconnect: str


def _interconnect(xml: bytes, product: str) -> str | None:
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml)
    for entry in root.iter("statusfordevice"):
        node = entry.find("device/product")
        if node is not None and node.get("name") == product:
            inter = entry.find("device/interconnect")
            return inter.get("name") if inter is not None else None
    return None


def select_drive(cfg: Config, drive_name: str | None, wait: float) -> Selected:
    drives = [cfg.drive(drive_name)] if drive_name else list(cfg.drives)
    if not drives:
        raise EnvironmentFailure("no drives configured", code="no-drive")
    deadline = time.monotonic() + max(0.0, wait)
    announced = False
    while True:
        candidates: list[Selected] = []
        for drive in drives:
            location = macos.find_drive(drive.vendor, drive.product)
            if location is None:
                continue
            code, xml = macos.drutil(drive.product, "status")
            if code != 0 or not xml.strip():
                continue
            status = macos.parse_drutil_status(xml, drive.product)
            if status is None or not status.present or status.in_transition:
                continue
            candidates.append(
                Selected(
                    drive,
                    location,
                    status,
                    _interconnect(xml, drive.product) or location.interconnect,
                )
            )
        if candidates:
            for candidate in candidates:
                if candidate.status.descriptor().family in candidate.drive.media:
                    return candidate
            return candidates[0]
        if time.monotonic() >= deadline:
            raise MediumRejected(
                "no medium found in any configured drive", code="no-medium"
            )
        if not announced:
            click.echo(
                f"waiting up to {int(wait)} s for a medium in "
                f"{', '.join(d.name for d in drives)} …"
            )
            announced = True
        time.sleep(2)


def check_known_bad(cfg: Config, location: macos.DriveLocation, build: str) -> None:
    if location.bridge is None:
        return
    for entry in cfg.known_bad:
        if (
            entry.bridge.upper() == location.bridge.key.upper()
            and build in entry.builds
        ):
            raise EnvironmentFailure(
                f"known-bad combination: bridge {location.bridge.key} with "
                f"redumper {build}: {entry.reason}",
                code="known-bad-combination",
            )


def check_firmware(selected: Selected) -> None:
    if selected.location.firmware != selected.drive.firmware:
        raise EnvironmentFailure(
            f"drive {selected.drive.name} reports firmware "
            f"{selected.location.firmware}, configured "
            f"{selected.drive.firmware}; verify the drive and update the configuration",
            code="firmware-mismatch",
        )


def test_key(selected: Selected, build: str) -> str:
    loc = selected.location
    return drive_test_key(
        loc.vendor,
        loc.product,
        loc.firmware,
        selected.interconnect,
        loc.bridge.key if loc.bridge else None,
        build,
    )


def cached_drive_test(
    cfg: Config, selected: Selected, build: str
) -> tuple[dict[str, Any], Path]:
    key = test_key(selected, build)
    directory = drive_tests_dir(cfg.work_root)
    path = directory / f"{key}.json"
    if not path.exists():
        raise EnvironmentFailure(
            "no drive::test result for this drive, firmware, connection and "
            "redumper build: insert a known error-free mixed-mode CD and run: "
            f"copyofa drive-test --drive {selected.drive.name}",
            code="drive-test-missing",
        )
    result: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if result.get("exit_code") != 0 or (
        selected.location.vendor == "PLEXTOR" and result.get("plextor_leadin") != "yes"
    ):
        raise EnvironmentFailure(
            f"cached drive::test failed (exit {result.get('exit_code')}, "
            f"PLEXTOR lead-in: {result.get('plextor_leadin')}); rerun "
            f"copyofa drive-test --drive {selected.drive.name}",
            code="drive-test-failed",
        )
    return result, directory / f"{key}.console"


def environment_record(selected: Selected, redumper: str, build: str) -> dict[str, Any]:
    loc = selected.location
    return {
        "drive_name": selected.drive.name,
        "vendor": loc.vendor,
        "product": loc.product,
        "firmware": loc.firmware,
        "interconnect": selected.interconnect,
        "bridge": (
            {
                "vendor_id": loc.bridge.vendor_id,
                "product_id": loc.bridge.product_id,
                "name": loc.bridge.name,
                "serial": loc.bridge.serial,
            }
            if loc.bridge
            else None
        ),
        "features": loc.features,
        "redumper_path": redumper,
        "build": build,
        "read_offset": None,
        "pregap_start": None,
        "drive_test": None,
    }


# --- identification -------------------------------------------------------------------


def preflight_entry(
    name: str,
    argv: list[str],
    run_result: StageRun | dict[str, Any],
    record: Path,
    cached_at: str | None = None,
) -> dict[str, Any]:
    return {
        "nn": None,
        "name": name,
        "kind": "preflight",
        "label": None,
        "argv": list(argv),
        "command_line": run_result["command_line"]
        if isinstance(run_result, dict)
        else run_result.command_line,
        "started_at": run_result["started_at"]
        if isinstance(run_result, dict)
        else run_result.started_at,
        "ended_at": run_result["ended_at"]
        if isinstance(run_result, dict)
        else run_result.ended_at,
        "exit_code": run_result["exit_code"]
        if isinstance(run_result, dict)
        else run_result.exit_code,
        "interrupted": False
        if isinstance(run_result, dict)
        else run_result.interrupted,
        "record": record.name,
        "cached_at": cached_at,
        "counts_after": None,
        "reported": None,
    }


def pre_dump_issue(
    code: str,
    what: str,
    why: str,
    holon: str,
    medium: str,
    values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return Issue(
        code=code,
        severity="blocking",
        title=what,
        what=what,
        why=why,
        actions=[
            f"copyofa accept {holon} {medium} {code} --reason TEXT",
            f"copyofa discard {holon} {medium} --yes",
        ],
        acceptable=True,
        values=values or {},
    ).as_dict()


def first_identification(
    run: Run, selected: Selected, drive_test: dict[str, Any], drive_test_console: Path
) -> None:
    """Record and check the medium before anything else touches it."""
    paths, state, holon, medium = run.paths, run.state, run.holon, str(run.medium)
    paths.records.mkdir(parents=True, exist_ok=True)
    paths.dump.mkdir(parents=True, exist_ok=True)
    for stale in paths.records.glob(f"{run.image}.preflight-*"):
        stale.chmod(0o644)
        stale.unlink()
    drive = selected.drive
    drutil_argv = macos.drutil_argv(drive.product, "status")
    drutil_record = paths.preflight("drutil.txt")
    drutil_run = run_recorded(drutil_argv, drutil_record, echo=False)
    status = macos.parse_drutil_status(drutil_record.read_bytes(), drive.product)
    if drutil_run.exit_code != 0 or status is None or not status.present:
        raise MediumRejected("drutil reports no medium in the drive", code="no-medium")
    descriptor = status.descriptor()
    if descriptor.blank:
        raise MediumRejected("blank medium: nothing to archive", code="blank-medium")
    stages = [preflight_entry("drutil status", drutil_argv, drutil_run, drutil_record)]
    ioreg_argv = macos.ioreg_record_argv(selected.location)
    ioreg_record = paths.preflight("ioreg.txt")
    stages.append(
        preflight_entry(
            "ioreg",
            ioreg_argv,
            run_recorded(ioreg_argv, ioreg_record, echo=False),
            ioreg_record,
        )
    )
    location = macos.find_drive(drive.vendor, drive.product)
    if location is None or location.bsd_name is None:
        raise MediumRejected(
            "the drive reports no medium device (no BSD name)", code="no-medium"
        )
    selected.location = location
    # The stages must use the device checked here, as the resume path does.
    run.location = location
    issues: list[dict[str, Any]] = []
    mounts = macos.mounts_for(location.bsd_name)
    rw = [m for m in mounts if not m.read_only]
    if rw:
        mounts_text = ", ".join(
            f"{m.device} on {m.mountpoint} ({m.fstype})" for m in rw
        )
        issues.append(
            pre_dump_issue(
                "premounted-read-write",
                f"the medium was mounted read-write before copyofa took control: "
                f"{mounts_text}",
                "macOS may already have written to the medium",
                holon,
                medium,
                {"mounts": [m.__dict__ for m in rw]},
            )
        )
    for mount in mounts:
        if mount.read_only:
            click.echo(
                f"{mount.device} is mounted read-only on {mount.mountpoint}; "
                f"redumper will unmount it"
            )
    if descriptor.family == "unknown":
        raise MediumRejected(
            f"unreadable or unsupported medium ({descriptor.media_type})",
            code="unreadable-medium",
        )
    if descriptor.family not in drive.media:
        raise MediumRejected(
            f"{descriptor.family} media need a drive verified for {descriptor.family}; "
            f"{drive.name} reads {', '.join(drive.media)}",
            code="no-drive-for-family",
        )
    prefix = run.vocab.prefixes[run.medium.prefix]
    if descriptor.family not in prefix.media:
        raise MediumRejected(
            f"prefix {prefix.name} expects {', '.join(prefix.media)} but the "
            f"medium is {descriptor.family}",
            code="prefix-mismatch",
        )
    profile = profiles.match_profile(descriptor, prefix.name)
    if profile is None:
        raise MediumRejected(
            f"no acquisition profile for {descriptor.family} media", code="no-profile"
        )
    approved = profile.approved(run.cfg.approved_profiles)
    tool = f"redumper {run.build}"
    drive_text = f"{location.vendor} {location.product} {location.firmware}"
    gaps: list[dict[str, Any]] = []
    if not approved:
        issues.append(
            pre_dump_issue(
                "provisional-capture",
                f"no approved acquisition profile: {profile.key} is "
                f"{profile.status}; the archive will be provisional",
                "only media captured with an approved profile become permanent "
                "archives",
                holon,
                medium,
                {"profile": profile.key, "status": profile.status},
            )
        )
        gaps.append(
            {
                "description": (
                    f"no approved acquisition profile covers {descriptor.media_type}, "
                    f"{descriptor.sessions} session(s), "
                    f"data tracks: {'yes' if descriptor.data_tracks else 'no'}, "
                    f"disc status {descriptor.disc_status}"
                ),
                "cause": f"{profile.name} {profile.version} ({profile.status})",
                "tool": tool,
                "drive": drive_text,
            }
        )
    if prefix.partial:
        gaps.append(
            {
                "description": prefix.partial_reason,
                "cause": f"medium ID prefix {prefix.name}",
                "tool": tool,
                "drive": drive_text,
            }
        )
    gaps.extend(
        {
            "description": f"session {session} lead-in not captured",
            "cause": "redumper b753 PLEXTOR lead-in reader stops after session 1",
            "tool": tool,
            "drive": drive_text,
        }
        for session in range(2, descriptor.sessions + 1)
    )
    console_record = paths.preflight("drive-test.console")
    shutil.copyfile(drive_test_console, console_record)
    console_record.chmod(0o444)
    stages.append(
        preflight_entry(
            "drive::test",
            drive_test["argv"],
            drive_test,
            console_record,
            cached_at=drive_test["ended_at"],
        )
    )
    state["environment"]["drive_test"] = {
        "record": console_record.name,
        "plextor_leadin": drive_test.get("plextor_leadin"),
    }
    state["medium"] = descriptor.as_dict()
    state["prefix"] = prefix.name
    state["profile"] = {
        "name": profile.name,
        "version": profile.version,
        "status": profile.status,
        "approved": approved,
        "strict_q": profile.strict_q,
        "stages": [s.as_dict() for s in profile.stages],
    }
    state["pre_dump_issues"] = issues
    state["gaps"] = gaps
    state["stages"] = stages
    state["phase"] = "acquiring"


def check_resume(run: Run, selected: Selected) -> None:
    """On resume, the medium and environment must be the ones recorded."""
    state = run.state
    status = selected.status
    descriptor = status.descriptor()
    if descriptor.blank:
        raise MediumRejected("blank medium", code="blank-medium")
    recorded = state["medium"]
    tracks_now = [(t.number, t.session, t.start, t.size) for t in status.tracks]
    tracks_then = [
        (t["number"], t["session"], t["start"], t["size"]) for t in recorded["tracks"]
    ]
    if (descriptor.media_type, descriptor.sessions, tracks_now) != (
        recorded["media_type"],
        recorded["sessions"],
        tracks_then,
    ):
        raise MediumRejected(
            f"the medium in the drive is not the one recorded for {run.image} "
            f"(type, sessions or track layout differ)",
            code="medium-changed",
        )
    env = state["environment"]
    loc = selected.location
    bridge_now = loc.bridge.key if loc.bridge else None
    bridge_then = (
        f"{env['bridge']['vendor_id']}:{env['bridge']['product_id']}"
        if env["bridge"]
        else None
    )
    if (loc.vendor, loc.product, loc.firmware, bridge_now, run.build) != (
        env["vendor"],
        env["product"],
        env["firmware"],
        bridge_then,
        env["build"],
    ):
        raise EnvironmentFailure(
            "drive, firmware, connection or redumper build differ from the "
            "recorded acquisition environment",
            code="environment-changed",
        )
    if loc.bsd_name:
        rw = [m for m in macos.mounts_for(loc.bsd_name) if not m.read_only]
        accepted = {a["code"] for a in state["acceptances"]}
        if rw and "premounted-read-write" not in accepted:
            issue = pre_dump_issue(
                "premounted-read-write",
                f"the medium is mounted read-write: {', '.join(m.device for m in rw)}",
                "macOS may already have written to the medium",
                run.holon,
                str(run.medium),
            )
            if all(
                i["code"] != "premounted-read-write" for i in state["pre_dump_issues"]
            ):
                state["pre_dump_issues"].append(issue)
    plan = state["profile"]["stages"]
    stage = (
        plan[state["plan_index"]]["command"]
        if state["plan_index"] < len(plan)
        else None
    )
    last = next_nn(state) - 1
    failed = (
        next((s for s in reversed(state["stages"]) if s["nn"] == last), None)
        if last
        else None
    )
    reason = (
        "repeat failed or interrupted stage"
        if failed and (failed["exit_code"] or failed["interrupted"])
        else "resume"
    )
    state["resumes"].append(
        {
            "resumed_at": utc_now(),
            "stage": stage,
            "previous_nn": last or None,
            "reason": reason,
        }
    )


def stop_on_pre_dump_issues(run: Run) -> None:
    issues = [Issue.from_dict(i) for i in run.state["pre_dump_issues"]]
    blocking = open_blocking(issues, run.state["acceptances"])
    if blocking:
        run.state["phase"] = "blocked"
        run.save()
        for issue in blocking:
            click.echo(render(issue))
        raise Stop(EXIT_FAIL)


# --- stages ---------------------------------------------------------------------------


def counts_dict(counts: rawdata.Counts | None) -> dict[str, Any] | None:
    if counts is None:
        return None
    return {
        "scsi_samples": counts.scsi_samples,
        "c2_samples": counts.c2_samples,
        "q_sectors": counts.q_sectors,
        "q_program_sectors": counts.q_program_sectors,
        "q_leadout_sectors": counts.q_leadout_sectors,
    }


def compute_counts(run: Run) -> dict[str, Any] | None:
    env = run.state["environment"]
    state_file = run.paths.dump / f"{run.image}.state"
    subcode = run.paths.dump / f"{run.image}.subcode"
    if (
        not (state_file.exists() and subcode.exists())
        or env["pregap_start"] is None
        or run.state["leadout_lba"] is None
    ):
        return None
    return counts_dict(
        rawdata.count_errors(
            state_file,
            subcode,
            env["pregap_start"],
            env["read_offset"],
            run.state["leadout_lba"],
        )
    )


def check(
    id_: str,
    ok: bool,
    values: dict[str, Any],
    note: str = "",
    origin: str = "acquisition",
) -> dict[str, Any]:
    return {
        "id": id_,
        "origin": origin,
        "status": "pass" if ok else "fail",
        "values": values,
        "note": note,
    }


def previous_counts(state: dict[str, Any]) -> dict[str, Any] | None:
    for entry in reversed(state["stages"]):
        if entry["kind"] == "acquisition" and entry.get("counts_after") is not None:
            counts: dict[str, Any] = entry["counts_after"]
            return counts
    return None


def run_stage(
    run: Run,
    command: str,
    label: str | None,
    options: list[str],
) -> dict[str, Any]:
    """Run one acquisition stage and record it; raises Stop when acquisition cannot
    continue."""
    state, paths = run.state, run.paths
    nn = next_nn(state)
    options = list(options)
    if command == "dump" and (paths.dump / f"{run.image}.state").exists():
        options.append("--overwrite")
        state["resumes"].append(
            {
                "resumed_at": utc_now(),
                "stage": "dump",
                "previous_nn": nn - 1,
                "reason": "repeat interrupted dump",
            }
        )
    assert run.location is not None  # archive and refine take the drive first
    argv = build_argv(
        run.redumper,
        command,
        image_path=paths.dump,
        image_name=run.image,
        drive_bsd=run.location.bsd_name,
        options=options,
    )
    record = paths.stage_record(nn, command)
    click.echo(
        f"--- stage {nn:02d}: {command}{f' ({label})' if label else ''}: "
        f"{' '.join(argv)}"
    )
    before = previous_counts(state)
    result = run_recorded(argv, record, cwd=paths.dump)
    lines = console.clean(record.read_bytes())
    sections = console.sections(lines)
    entry = {
        "nn": nn,
        "name": command,
        "kind": "acquisition",
        "label": label,
        "argv": result.argv,
        "command_line": result.command_line,
        "started_at": result.started_at,
        "ended_at": result.ended_at,
        "exit_code": result.exit_code,
        "interrupted": result.interrupted,
        "record": record.name,
        "cached_at": None,
        "counts_after": None,
        "reported": None,
    }
    state["stages"].append(entry)
    env = state["environment"]
    header = console.parse_header(lines)
    if command == "dump" and header and header.drive and env["pregap_start"] is None:
        env["read_offset"] = header.drive.read_offset
        env["pregap_start"] = header.drive.pregap_start
    body = sections.get(command.upper(), [])
    if command == "dump":
        toc = console.parse_disc_toc(body)
        if toc and toc.leadout_lba is not None and state["leadout_lba"] is None:
            state["leadout_lba"] = toc.leadout_lba
        entry["reported"] = counts_dict(console.parse_media_errors(body))
    elif command == "refine":
        entry["reported"] = counts_dict(console.parse_correction(body))
    entry["counts_after"] = compute_counts(run)
    classified = run.catalog.classify(record.name, lines)
    state["log_messages"] = [
        m for m in state["log_messages"] if m["record"] != record.name
    ] + [
        {"record": c.record, "line": c.line, "class": c.cls, "code": c.code}
        for c in classified
    ]
    failed = result.exit_code != 0 or result.interrupted
    if not failed:
        add_count_checks(run, entry, before)
    if command == "dump" and not failed:
        set_first_pass(run, entry)
    hard = [
        catalog_issue(c, run.holon, str(run.medium))
        for c in classified
        if c.cls == "blocking" and not c.acceptable
    ]
    if failed:
        hard.append(
            stage_failed_issue(
                run.holon,
                str(run.medium),
                command,
                nn,
                result.exit_code,
                result.interrupted,
            )
        )
    if hard:
        state["issues"] = [i.as_dict() | {"accepted": False} for i in hard]
        state["phase"] = "blocked"
        run.save()
        for issue in hard:
            click.echo(render(issue))
        raise Stop(EXIT_FAIL)
    run.save()
    return entry


def add_count_checks(
    run: Run, entry: dict[str, Any], before: dict[str, Any] | None
) -> None:
    """Redumper's reported counts must equal the counts recomputed from
    .state/.subcode."""
    after, reported = entry["counts_after"], entry["reported"]
    checks = run.state["acquisition_checks"]
    fields = ("scsi_samples", "c2_samples", "q_sectors")
    if entry["name"] == "dump":
        if after is None or reported is None:
            checks.append(
                {
                    "id": "dump-counts",
                    "origin": "acquisition",
                    "status": "skip",
                    "values": {"reported": reported, "computed": after},
                    "note": "counts unavailable",
                }
            )
            return
        ok = all(reported[f] == after[f] for f in fields)
        checks.append(
            check(
                "dump-counts",
                ok,
                {"reported": reported, "computed": after},
                "dump media errors vs counts recomputed from .state/.subcode",
            )
        )
    elif entry["name"] == "refine":
        cid = f"refine-counts-{entry['nn']:02d}"
        if after is None or reported is None or before is None:
            checks.append(
                {
                    "id": cid,
                    "origin": "acquisition",
                    "status": "skip",
                    "values": {"reported": reported, "before": before, "after": after},
                    "note": "counts unavailable",
                }
            )
            return
        corrected = {f: before[f] - after[f] for f in fields}
        ok = all(corrected[f] == reported[f] for f in fields)
        checks.append(
            check(
                cid,
                ok,
                {
                    "reported": reported,
                    "before": before,
                    "after": after,
                    "corrected": corrected,
                },
                "refine correction statistics vs before/after counts from "
                ".state/.subcode",
            )
        )


def set_first_pass(run: Run, entry: dict[str, Any]) -> None:
    state = run.state
    reported, after = entry["reported"] or {}, entry["counts_after"] or {}
    program_sectors = None
    if (
        state["leadout_lba"] is not None
        and state["environment"]["pregap_start"] is not None
    ):
        program_sectors = state["leadout_lba"] - state["environment"]["pregap_start"]
    q_program = after.get("q_program_sectors")
    rate = (
        q_program / program_sectors
        if q_program is not None and program_sectors
        else None
    )
    state["first_pass"] = {
        "scsi_samples": reported.get("scsi_samples"),
        "c2_samples": reported.get("c2_samples"),
        "q_sectors": reported.get("q_sectors"),
        "q_program_sectors": q_program,
        "q_leadout_sectors": after.get("q_leadout_sectors"),
        "program_sectors": program_sectors,
        "q_program_rate": rate,
    }
    medium = state["medium"]
    threshold = run.cfg.q_rate_threshold
    if medium["recordable"] or medium["rewritable"]:
        enabled, reason = True, "recordable or rewritable medium"
    elif (reported.get("scsi_samples") or 0) > 0 or (
        reported.get("c2_samples") or 0
    ) > 0:
        enabled, reason = True, "SCSI or C2 errors in the first dump pass"
    elif rate is not None and rate > threshold:
        enabled, reason = (
            True,
            f"first-pass program-area Q error rate {rate:.4%} above {threshold:.4%}",
        )
    else:
        enabled, reason = False, "pressed medium without first-pass read problems"
    state["redundancy"]["default_enabled"] = enabled
    state["redundancy"]["default_reason"] = reason


def stage_should_run(state: dict[str, Any], stage: dict[str, Any]) -> bool:
    if stage.get("when") == "c2_or_scsi_remaining":
        counts = previous_counts(state)
        return counts is None or counts["scsi_samples"] > 0 or counts["c2_samples"] > 0
    return True


def run_acquisition(run: Run) -> None:
    """Run the profile's stages from plan_index, then snapshot the raw files."""
    state = run.state
    plan = state["profile"]["stages"]
    while state["plan_index"] < len(plan):
        stage = plan[state["plan_index"]]
        if stage_should_run(state, stage):
            run_stage(
                run, stage["command"], stage.get("label"), stage.get("options", [])
            )
        else:
            state["skipped_stages"].append(
                {
                    "command": stage["command"],
                    "label": stage.get("label"),
                    "reason": "no SCSI or C2 errors remaining",
                }
            )
            click.echo(
                f"--- skipping {stage['command']} ({stage.get('label')}): no "
                f"SCSI or C2 errors remaining"
            )
        state["plan_index"] += 1
        run.save()
    finish_acquisition(run)


def finish_acquisition(run: Run) -> None:
    state = run.state
    state["raw_snapshot"] = snapshot(run.paths.dump)
    set_read_only(run.paths.dump, True)
    state["acquisition_complete"] = True
    state["verification_needed"] = True
    state["phase"] = "verifying"
    run.save()


# --- top-level commands ---------------------------------------------------------------


def _lock_work(run: Run) -> None:
    run.paths.root.mkdir(parents=True, exist_ok=True)
    run.exit_stack.enter_context(macos.FileLock(run.paths.lock))
    run.state = load_state(run.paths) or new_state(run.holon, str(run.medium))


def _take_drive(run: Run, drive_name: str | None, wait: float) -> Selected:
    cfg, state = run.cfg, run.state
    run.exit_stack.enter_context(macos.MountGuard(macos.drive_predicate(cfg.drives)))
    if (
        state.get("environment")
        and drive_name
        and drive_name != state["environment"]["drive_name"]
    ):
        raise UsageError(
            f"{run.image} is being acquired with drive "
            f"{state['environment']['drive_name']}"
        )
    if state.get("environment"):
        drive_name = state["environment"]["drive_name"]
    selected = select_drive(cfg, drive_name, wait)
    check_known_bad(cfg, selected.location, run.build)
    check_firmware(selected)
    loc = selected.location
    run.exit_stack.enter_context(
        macos.FileLock(
            drive_lock_path(cfg.work_root, loc.vendor, loc.product, loc.firmware)
        )
    )
    run.drive = selected.drive
    run.location = selected.location
    return selected


def _apply_redundancy(run: Run, redundancy: str | None, reason: str | None) -> None:
    if redundancy is None:
        return
    if not reason:
        raise UsageError("--redundancy needs --redundancy-reason")
    run.state["redundancy"]["override"] = {
        "value": redundancy == "on",
        "reason": reason,
    }


def archive(
    cfg: Config,
    holon_text: str,
    medium_text: str,
    *,
    media_list: str | None = None,
    drive_name: str | None = None,
    wait: float = 300,
    redundancy: str | None = None,
    redundancy_reason: str | None = None,
) -> int:
    run = open_run(cfg, holon_text, medium_text)
    with run.exit_stack:
        _lock_work(run)
        fresh = run.state.get("medium") is None
        try:
            check_product(run, media_list)
            _apply_redundancy(run, redundancy, redundancy_reason)
            run.redumper, run.build = environment_preflight(cfg)
            if not run.state["acquisition_complete"]:
                selected = _take_drive(run, drive_name, wait)
                if fresh:
                    drive_test, drive_test_console = cached_drive_test(
                        cfg, selected, run.build
                    )
                    run.state["environment"] = environment_record(
                        selected, run.redumper, run.build
                    )
                    first_identification(run, selected, drive_test, drive_test_console)
                else:
                    check_resume(run, selected)
                    run.location = (
                        macos.find_drive(selected.drive.vendor, selected.drive.product)
                        or selected.location
                    )
                run.save()
                stop_on_pre_dump_issues(run)
                run_acquisition(run)
            else:
                run.save()
        except (MediumRejected, EnvironmentFailure, UsageError) as exc:
            if fresh and run.state.get("medium") is None:
                make_tree_writable(run.paths.root)
                shutil.rmtree(run.paths.root, ignore_errors=True)
            raise exc
        except Stop as stop:
            return stop.exit_code
        from .publication import verify_analyse_publish

        return verify_analyse_publish(run)


def refine(
    cfg: Config,
    holon_text: str,
    medium_text: str,
    *,
    retries: int,
    speed: float | None = None,
    refine_subchannel: bool = False,
) -> int:
    run = open_run(cfg, holon_text, medium_text)
    with run.exit_stack:
        _lock_work(run)
        state = run.state
        if not state.get("acquisition_complete"):
            raise UsageError(
                f"acquisition of {run.image} is not complete: run copyofa archive "
                f"{run.holon} {run.medium}"
            )
        if state["phase"] in ("publishing", "published"):
            raise UsageError(f"{run.image} is already being published")
        check_product(run, None)
        run.redumper, run.build = environment_preflight(cfg)
        selected = _take_drive(run, None, 0)
        check_resume(run, selected)
        run.location = (
            macos.find_drive(selected.drive.vendor, selected.drive.product)
            or selected.location
        )
        options = [f"--retries={retries}"]
        if speed is not None:
            options.append(f"--speed={speed:g}")
        if refine_subchannel:
            options.append("--refine-subchannel")
        set_read_only(run.paths.dump, False)
        try:
            run_stage(run, "refine", "extra", options)
        except Stop as stop:
            return stop.exit_code
        finally:
            set_read_only(
                run.paths.dump, True
            )  # never leave the acquired raw files writable
        finish_acquisition(run)
        from .publication import verify_analyse_publish

        return verify_analyse_publish(run)


def accept(
    cfg: Config, holon_text: str, medium_text: str, code: str, reason: str
) -> int:
    run = open_run(cfg, holon_text, medium_text)
    with run.exit_stack:
        if not run.paths.state_file.exists():
            raise UsageError(f"no work in progress for {run.image}")
        _lock_work(run)
        state = run.state
        if not reason or not reason.strip():
            raise UsageError("an acceptance needs a non-empty --reason")
        accepted = {a["code"] for a in state["acceptances"]}
        open_issues = [
            i
            for i in state["issues"] + state["pre_dump_issues"]
            if i["severity"] == "blocking"
            and i["acceptable"]
            and i["code"] not in accepted
        ]
        match = next((i for i in open_issues if i["code"] == code), None)
        if match is None:
            codes = ", ".join(sorted({i["code"] for i in open_issues})) or "none"
            raise UsageError(
                f"{code} is not an open acceptable issue of {run.image}; open "
                f"acceptable issues: {codes}"
            )
        state["acceptances"].append(
            {
                "code": code,
                "title": match["title"],
                "accepted_at": utc_now(),
                "reason": reason.strip(),
            }
        )
        if code == "split-refused":
            state["force_split"] = True
            state["verification_needed"] = True
        save_state(run.paths, state)
        click.echo(f"accepted {code} for {run.image}")
        click.echo(f"rerun: copyofa archive {run.holon} {run.medium}")
        return 0


def discard(cfg: Config, holon_text: str, medium_text: str, *, yes: bool) -> int:
    run = open_run(cfg, holon_text, medium_text)
    if not run.paths.root.exists():
        raise UsageError(f"no work directory for {run.image}")
    with run.exit_stack:
        _lock_work(run)
        if run.state.get("phase") == "publishing":
            raise UsageError(
                f"{run.image} is being published: run copyofa recover {run.holon} first"
            )
        if not yes:
            if sys.stdin is None or not sys.stdin.isatty():
                raise UsageError(
                    f"discard of {run.image} needs --yes when stdin is not a terminal"
                )
            try:
                confirmed = click.confirm(
                    f"Delete the work directory {run.paths.root}?"
                )
            except click.Abort:  # Ctrl-C / Ctrl-D at the prompt
                confirmed = False
            if not confirmed:
                raise UsageError(f"discard of {run.image} canceled; nothing deleted")
        make_tree_writable(run.paths.root)
        shutil.rmtree(run.paths.root)
    click.echo(f"discarded {run.paths.root}")
    return 0


def drive_test(cfg: Config, drive_name: str | None = None, wait: float = 300) -> int:
    redumper, build = environment_preflight(cfg)
    with contextlib.ExitStack() as stack:
        stack.enter_context(macos.MountGuard(macos.drive_predicate(cfg.drives)))
        selected = select_drive(cfg, drive_name, wait)
        check_known_bad(cfg, selected.location, build)
        check_firmware(selected)
        loc = selected.location
        stack.enter_context(
            macos.FileLock(
                drive_lock_path(cfg.work_root, loc.vendor, loc.product, loc.firmware)
            )
        )
        if selected.status.descriptor().blank:
            raise MediumRejected(
                "blank medium: drive::test needs an error-free mixed-mode CD",
                code="blank-medium",
            )
        location = macos.find_drive(loc.vendor, loc.product)
        if location is None or location.bsd_name is None:
            raise MediumRejected("the drive reports no medium device", code="no-medium")
        if any(not m.read_only for m in macos.mounts_for(location.bsd_name)):
            raise MediumRejected(
                "the medium is mounted read-write; eject it and use another disc",
                code="premounted-read-write",
            )
        key = test_key(selected, build)
        directory = drive_tests_dir(cfg.work_root)
        directory.mkdir(parents=True, exist_ok=True)
        for ext in ("console", "log", "json"):
            old = directory / f"{key}.{ext}"
            if old.exists():
                old.chmod(0o644)
                old.unlink()
        argv = build_argv(
            redumper,
            "drive::test",
            image_path=directory,
            image_name=key,
            drive_bsd=location.bsd_name,
        )
        result = run_recorded(argv, directory / f"{key}.console", cwd=directory)
        leadin = console.parse_drive_test(
            console.clean((directory / f"{key}.console").read_bytes())
        )
        atomic_write_json(
            directory / f"{key}.json",
            {
                "vendor": loc.vendor,
                "product": loc.product,
                "firmware": loc.firmware,
                "interconnect": selected.interconnect,
                "bridge": loc.bridge.key if loc.bridge else None,
                "build": build,
                "argv": result.argv,
                "command_line": result.command_line,
                "started_at": result.started_at,
                "ended_at": result.ended_at,
                "exit_code": result.exit_code,
                "plextor_leadin": leadin,
            },
        )
        ok = result.exit_code == 0 and (loc.vendor != "PLEXTOR" or leadin == "yes")
        click.echo(
            f"drive::test {'passed' if ok else 'FAILED'} (exit {result.exit_code}, "
            f"PLEXTOR lead-in: {leadin})"
        )
        return 0 if ok else 22
