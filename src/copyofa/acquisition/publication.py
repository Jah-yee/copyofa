"""Verification run, analysis and publication for `coa archive` and `coa refine`."""

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from ..archive import analysis
from ..errors import (
    EXIT_ACCEPTED,
    EXIT_FAIL,
    EXIT_PASS,
    EnvironmentFailure,
    MediumRejected,
)
from ..issues import ACCEPTED, FAIL, PASS, Issue, open_blocking, outcome, render
from ..util import parse_utc, utc_now
from .verifyrun import run_verification
from .workstate import make_tree_writable, save_state

if TYPE_CHECKING:
    from .acquire import Run


def metadata_stages(state: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for s in state["stages"]:
        duration = None
        if s.get("started_at") and s.get("ended_at"):
            duration = (
                parse_utc(s["ended_at"]) - parse_utc(s["started_at"])
            ).total_seconds()
        result.append(
            {
                "nn": s["nn"],
                "name": s["name"],
                "kind": s["kind"],
                "label": s["label"],
                "command_line": s["command_line"],
                "argv": s["argv"],
                "started_at": s["started_at"],
                "ended_at": s["ended_at"],
                "duration_s": duration,
                "exit_code": s["exit_code"],
                "interrupted": s["interrupted"],
                "record": s["record"],
                "cached_at": s["cached_at"],
            }
        )
    return result


def _option(argv: list[str], name: str) -> str | None:
    for arg in argv:
        if arg.startswith(f"--{name}="):
            return arg.split("=", 1)[1]
    return None


def acquisition_errors(state: dict[str, Any]) -> dict[str, Any]:
    env = state["environment"] or {}
    drive = " ".join(
        x for x in (env.get("vendor"), env.get("product"), env.get("firmware")) if x
    )
    passes = []
    before = None
    for s in state["stages"]:
        if s["kind"] != "acquisition":
            continue
        if s["name"] == "refine" and s["exit_code"] == 0 and not s["interrupted"]:
            retries = _option(s["argv"], "retries")
            speed = _option(s["argv"], "speed")
            passes.append(
                {
                    "nn": s["nn"],
                    "label": s["label"],
                    "retries": int(retries) if retries is not None else None,
                    "speed": float(speed) if speed is not None else None,
                    "refine_subchannel": "--refine-subchannel" in s["argv"],
                    "drive": drive,
                    "before": before,
                    "after": s["counts_after"],
                    "reported": s["reported"],
                }
            )
        if s["counts_after"] is not None and s["exit_code"] == 0:
            before = s["counts_after"]
    return {
        "first_pass": state["first_pass"],
        "passes": passes,
        "cumulative_max_attempts": {
            "c2_sectors": 1 + sum(p["retries"] or 0 for p in passes),
            "q_sectors": 1
            + sum(p["retries"] or 0 for p in passes if p["refine_subchannel"]),
        },
    }


def recorded_inputs(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "stages": metadata_stages(state),
        "accepted_issues": state["acceptances"],
        "profile": state["profile"],
        "checks": state["acquisition_checks"],
        "acquisition_errors": acquisition_errors(state),
    }


def raw_map(run: Run) -> dict[str, Path]:
    raw = {p.name: p for p in sorted(run.paths.dump.iterdir()) if p.is_file()}
    raw.update({p.name: p for p in sorted(run.paths.records.iterdir()) if p.is_file()})
    return raw


def analyse(run: Run) -> dict[str, Any]:
    """Derive every re-derivable field from the work files; store the issues in
    the state."""
    state = run.state
    derived = analysis.derive(
        run.holon, str(run.medium), raw_map(run), recorded_inputs(state), run.catalog
    )
    by_name = {f["name"]: f for f in derived["raw_files"]}
    current = {
        name: (f["size"], f["sha256"])
        for name, f in by_name.items()
        if (run.paths.dump / name).exists()
    }
    snap = {name: (s["size"], s["sha256"]) for name, s in state["raw_snapshot"].items()}
    changed = sorted(
        name for name in set(current) | set(snap) if current.get(name) != snap.get(name)
    )
    if changed:
        derived["issues"].append(
            Issue(
                "raw-modified",
                "blocking",
                "raw files changed",
                f"raw files differ from the snapshot taken after acquisition: "
                f"{', '.join(changed)}",
                "the raw dump must stay as acquired",
                [f"coa discard {run.holon} {run.medium} --yes and re-dump"],
                False,
                {"changed": changed},
            ).as_dict()
            | {"accepted": False}
        )
    state["issues"] = derived["issues"]
    save_state(run.paths, state)
    return derived


def report(run: Run, derived: dict[str, Any]) -> list[Issue]:
    issues = [Issue.from_dict(i) for i in derived["issues"]]
    accepted = {a["code"] for a in run.state["acceptances"]}
    for issue in issues:
        if issue.severity == "blocking" and issue.code in accepted and issue.acceptable:
            click.echo(f"ACCEPTED {issue.code}: {issue.what}")
        else:
            click.echo(render(issue))
    return issues


def run_outcome(run: Run, issues: list[Issue]) -> str:
    accepted_now = list(run.state["acceptances"])
    pre = [Issue.from_dict(i) for i in run.state["pre_dump_issues"]]
    return outcome(issues + pre, accepted_now)


def verify_analyse_publish(run: Run) -> int:
    state = run.state
    if state["verification_needed"]:
        state["phase"] = "verifying"
        run_verification(run)
    derived = analyse(run)
    issues = report(run, derived)
    result = run_outcome(run, issues)
    if result == FAIL:
        state["phase"] = "blocked"
        save_state(run.paths, state)
        blocking = open_blocking(issues, state["acceptances"])
        click.echo(f"{len(blocking)} blocking issue(s) open for {run.image}")
        return EXIT_FAIL
    return publish(run, derived, result)


def gaps_for_publication(state: dict[str, Any]) -> list[dict[str, Any]]:
    gaps = list(state["gaps"])
    if any(a["code"] == "leadin-not-verified" for a in state["acceptances"]):
        env = state["environment"]
        gaps.append(
            {
                "description": "session 1 lead-in not captured",
                "cause": "lead-in not stored as verified; accepted by the owner",
                "tool": f"redumper {env['build']}",
                "drive": f"{env['vendor']} {env['product']} {env['firmware']}",
            }
        )
    return gaps


def publish(run: Run, derived: dict[str, Any], result: str) -> int:
    """Build the medium set and publish it through the transaction engine."""
    from ..archive import archiveset, metadata
    from ..archive import product as product_mod
    from ..archive.transaction import Transaction, recover
    from ..system.macos import FileLock

    cfg, state = run.cfg, run.state
    cfg.archive_root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(cfg.archive_root).free
    if free < cfg.min_free_archive_bytes:
        raise EnvironmentFailure(
            f"only {free} bytes free on the archive volume, "
            f"{cfg.min_free_archive_bytes} required",
            code="low-disk-space",
        )
    product_dir = cfg.archive_root / run.holon
    product_dir.mkdir(exist_ok=True)
    with FileLock(product_dir / "product.lock"):
        recover(product_dir, cfg.work_root)
        existing = (
            product_mod.read_product(product_dir)
            if (product_dir / "product.json").exists()
            else None
        )
        declared = state["declared_media_list"]
        if existing:
            entry = next(
                (m for m in existing["media"] if m["medium_id"] == str(run.medium)),
                None,
            )
            if entry is None:
                raise MediumRejected(
                    f"{run.medium} is not in the media list of {run.holon}",
                    code="not-in-media-list",
                )
            if entry["status"] == "archived":
                raise MediumRejected(
                    f"{run.holon} {run.medium} is already archived",
                    code="already-archived",
                )
            current = [m["medium_id"] for m in existing["media"]]
            if declared and current != declared:
                amend_action = (
                    f"coa amend {run.holon} --media-list "
                    f"{','.join(declared)} --reason TEXT"
                )
                issue = Issue(
                    "media-list-conflict",
                    "blocking",
                    "media list conflict",
                    f"product.json lists {', '.join(current)} but "
                    f"{', '.join(declared)} was declared for this medium",
                    "the product manifest holds the one declared media list",
                    [
                        amend_action,
                        f"coa discard {run.holon} {run.medium} --yes",
                    ],
                    False,
                    {},
                )
                click.echo(render(issue))
                state["issues"].append(issue.as_dict() | {"accepted": False})
                save_state(run.paths, state)
                return EXIT_FAIL
            media_list = current
            product_doc = existing
        else:
            media_list = declared
            product_doc = product_mod.new_product(run.holon, media_list, run.vocab)
        doc = metadata.build_medium(
            cfg=cfg,
            run=run,
            derived=derived,
            media_list=media_list,
            outcome=result,
            gaps=gaps_for_publication(state),
            stages=metadata_stages(state),
            acquisition_errors=acquisition_errors(state),
        )
        new_product = product_mod.with_archived(product_doc, str(run.medium), doc)
        raw = raw_map(run)
        state["phase"] = "publishing"
        save_state(run.paths, state)

        def build(staging: Path) -> None:
            archiveset.build_set(staging, metadata=doc, raw=raw)
            product_mod.write_product_files(staging, new_product)

        Transaction(product_dir, "publish", run.holon, str(run.medium), cfg=cfg).run(
            build
        )
        state["phase"] = "published"
        save_state(run.paths, state)
        click.echo(product_mod.completeness_line(new_product))
    make_tree_writable(run.paths.root)
    shutil.rmtree(run.paths.root)
    click.echo(f"published {run.image} ({result}) at {utc_now()}")
    return (
        EXIT_PASS
        if result == PASS
        else EXIT_ACCEPTED
        if result == ACCEPTED
        else EXIT_FAIL
    )
