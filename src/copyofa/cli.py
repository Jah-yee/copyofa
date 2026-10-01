"""Command-line interface `coa`."""

import traceback
from pathlib import Path
from typing import NoReturn

import click

from . import __version__, config
from .acquisition import acquire
from .acquisition.workstate import WorkPaths, load_state
from .archive import product as product_mod
from .errors import EXIT_ACCEPTED, EXIT_INTERNAL, EXIT_PASS, CoaError
from .ids import normalize_holon_id, parse_medium_id
from .issues import ACCEPTED, FAIL, PASS, Issue, open_blocking, render


def _outcome_line(code: int) -> str:
    return f"OUTCOME: {PASS if code == EXIT_PASS else ACCEPTED if code == EXIT_ACCEPTED else FAIL}"


class CoaCommand(click.Command):
    """Runs the callback (which returns an exit code), reports errors, prints the OUTCOME line when `outcome`
    is set, and exits with the code."""

    def __init__(self, *args, outcome: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.outcome = outcome

    def invoke(self, ctx: click.Context) -> NoReturn:
        try:
            code = super().invoke(ctx)
        except CoaError as exc:
            label = f" [{exc.code}]" if exc.code else ""
            click.echo(f"coa: {type(exc).__name__}{label}: {exc}", err=True)
            code = exc.exit_code
        except KeyboardInterrupt:
            click.echo("coa: interrupted", err=True)
            code = 130
        except Exception as exc:  # noqa: BLE001 - internal error
            click.echo(f"coa: internal error: {exc}", err=True)
            click.echo(traceback.format_exc(), err=True, nl=False)
            code = EXIT_INTERNAL
        if self.outcome:
            click.echo(_outcome_line(code))
        ctx.exit(code)


class CoaGroup(click.Group):
    command_class = CoaCommand


@click.group("coa", cls=CoaGroup, context_settings={"help_option_names": ["-h", "--help"]},
             help="Archive optical discs bit-exactly with redumper")
@click.option("--config", "config_path", metavar="PATH",
              help="config file (default $COA_CONFIG or ~/.config/copyofa/config.toml)")
@click.version_option(__version__, prog_name="coa", message="%(prog)s %(version)s")
@click.pass_context
def main(ctx: click.Context, config_path: str | None) -> None:
    ctx.obj = config_path


@main.command("init", help="write the config file and create the roots")
@click.option("--archive-root", type=click.Path(path_type=Path), required=True)
@click.option("--work-root", type=click.Path(path_type=Path), required=True)
@click.pass_obj
def init_cmd(config_path: str | None, archive_root: Path, work_root: Path) -> int:
    paths = config.init(config.resolve_path(config_path), archive_root, work_root)
    click.echo(f"config: {paths[0]}\narchive root: {paths[1]}\nwork root: {paths[2]}\nprefix vocabulary: {paths[3]}")
    return 0


@main.command("archive", outcome=True, help="acquire, verify and publish a medium (resumable)")
@click.argument("holon")
@click.argument("medium")
@click.option("--media-list", help="declared media list of the product (first medium only)")
@click.option("--drive", help="configured drive name")
@click.option("--wait", type=float, default=300, show_default=True, help="seconds to wait for a medium")
@click.option("--redundancy", type=click.Choice(["on", "off"]))
@click.option("--redundancy-reason")
@click.pass_obj
def archive_cmd(config_path: str | None, holon: str, medium: str, media_list: str | None, drive: str | None,
                wait: float, redundancy: str | None, redundancy_reason: str | None) -> int:
    return acquire.archive(config.load(config_path), holon, medium, media_list=media_list, drive_name=drive,
                           wait=wait, redundancy=redundancy, redundancy_reason=redundancy_reason)


@main.command("refine", outcome=True, help="run another refine pass after acquisition, then verify again")
@click.argument("holon")
@click.argument("medium")
@click.option("--retries", type=int, required=True)
@click.option("--speed", type=float)
@click.option("--refine-subchannel", is_flag=True)
@click.pass_obj
def refine_cmd(config_path: str | None, holon: str, medium: str, retries: int, speed: float | None,
               refine_subchannel: bool) -> int:
    return acquire.refine(config.load(config_path), holon, medium, retries=retries, speed=speed,
                          refine_subchannel=refine_subchannel)


@main.command("accept", help="accept an open blocking issue")
@click.argument("holon")
@click.argument("medium")
@click.argument("code")
@click.option("--reason", required=True)
@click.pass_obj
def accept_cmd(config_path: str | None, holon: str, medium: str, code: str, reason: str) -> int:
    return acquire.accept(config.load(config_path), holon, medium, code, reason)


@main.command("discard", help="delete the work directory of a medium")
@click.argument("holon")
@click.argument("medium")
@click.option("--yes", is_flag=True, help="delete without asking")
@click.pass_obj
def discard_cmd(config_path: str | None, holon: str, medium: str, yes: bool) -> int:
    return acquire.discard(config.load(config_path), holon, medium, yes=yes)


@main.command("drive-test", help="run and cache redumper drive::test")
@click.option("--drive", help="configured drive name")
@click.option("--wait", type=float, default=300, show_default=True, help="seconds to wait for a medium")
@click.pass_obj
def drive_test_cmd(config_path: str | None, drive: str | None, wait: float) -> int:
    return acquire.drive_test(config.load(config_path), drive, wait)


@main.command("status", help="show work in progress or a product")
@click.argument("holon", required=False)
@click.argument("medium", required=False)
@click.pass_obj
def status_cmd(config_path: str | None, holon: str | None, medium: str | None) -> int:
    cfg = config.load(config_path)
    if not holon:
        rows = []
        if cfg.work_root.exists():
            for d in sorted(cfg.work_root.iterdir()):
                if d.is_dir() and (d / "state.json").exists():
                    work_holon, _, work_medium = d.name.partition("-")
                    state = load_state(WorkPaths(cfg.work_root, work_holon, work_medium))
                    issues = [Issue.from_dict(i) for i in state["issues"] + state["pre_dump_issues"]]
                    codes = ", ".join(i.code for i in open_blocking(issues, state["acceptances"])) or "-"
                    rows.append(f"{d.name}  {state['phase']}  open: {codes}")
        click.echo("\n".join(rows) if rows else "no work in progress")
        return 0
    from .archive.transaction import journals

    holon = normalize_holon_id(holon)
    pd = cfg.archive_root / holon
    if (pd / "product.json").exists():
        doc = product_mod.read_product(pd)
        for entry in doc["media"]:
            extra = ""
            if entry["status"] == "archived":
                mdir = product_mod.medium_dir(pd, doc, entry["medium_id"])
                meta = mdir / f"{holon}-{entry['medium_id']}.json"
                partial = None
                if meta.exists():
                    import json

                    partial = json.loads(meta.read_text(encoding="utf-8")).get("partial")
                extra = (f"  {entry['permanence']}  {entry['outcome']}  {entry['archived_at']}"
                         + (f"  partial ({partial['prefix']})" if partial else ""))
            click.echo(f"{entry['medium_id']:8} {entry['status']}{extra}")
        click.echo(product_mod.completeness_line(doc))
        permanent, total, _ = product_mod.completeness(doc)
        if permanent < total:
            click.echo(f"ISSUE incomplete-product: {total - permanent} optical medium/media not permanently archived")
        for journal in journals(pd):
            click.echo(f"UNCOMMITTED {journal.relative_to(pd)}: run coa recover {holon}")
        for path in pd.iterdir():
            if path.name.startswith((".coa-staging-", ".coa-backup-")):
                click.echo(f"UNCOMMITTED leftover {path.name}: run coa recover {holon}")
    else:
        click.echo(f"{holon}: no product manifest")
    media = [str(parse_medium_id(medium))] if medium else None
    for d in sorted(cfg.work_root.glob(f"{holon}-*")) if cfg.work_root.exists() else []:
        work_medium = d.name.partition("-")[2]
        if media and work_medium not in media:
            continue
        state = load_state(WorkPaths(cfg.work_root, holon, work_medium))
        if state is None:
            continue
        click.echo(f"work {d.name}: {state['phase']}")
        if not medium:
            continue
        for s in state["stages"]:
            label = f" ({s['label']})" if s["label"] else ""
            nn = f"{s['nn']:02d}" if s["nn"] is not None else "--"
            click.echo(f"  {nn} {s['name']}{label}: exit {s['exit_code']}{' interrupted' if s['interrupted'] else ''}")
        for skipped in state["skipped_stages"]:
            click.echo(f"  skipped {skipped['command']} ({skipped['label']}): {skipped['reason']}")
        issues = [Issue.from_dict(i) for i in state["issues"] + state["pre_dump_issues"]]
        for issue in open_blocking(issues, state["acceptances"]):
            click.echo(render(issue))
        for acceptance in state["acceptances"]:
            click.echo(f"  accepted {acceptance['code']} at {acceptance['accepted_at']}: {acceptance['reason']}")
    return 0


@main.command("verify", help="verify a product (fast, or --deep); --repair uses PAR2")
@click.argument("holon")
@click.argument("medium", required=False)
@click.option("--deep", is_flag=True)
@click.option("--repair", is_flag=True)
@click.pass_obj
def verify_cmd(config_path: str | None, holon: str, medium: str | None, deep: bool, repair: bool) -> int:
    from .archive.verify import verify_command

    return verify_command(config.load(config_path), holon, medium, deep=deep, repair=repair)


@main.command("amend", help="change the media list of a product")
@click.argument("holon")
@click.option("--media-list", required=True)
@click.option("--reason", required=True)
@click.pass_obj
def amend_cmd(config_path: str | None, holon: str, media_list: str, reason: str) -> int:
    return product_mod.amend(config.load(config_path), holon, media_list, reason)


@main.command("reprocess", help="re-derive the metadata from the archived raw files and reseal")
@click.argument("holon")
@click.argument("medium")
@click.option("--reason", required=True)
@click.option("--redundancy", type=click.Choice(["on", "off"]))
@click.option("--redundancy-reason")
@click.pass_obj
def reprocess_cmd(config_path: str | None, holon: str, medium: str, reason: str, redundancy: str | None,
                  redundancy_reason: str | None) -> int:
    from .archive.reprocess import reprocess

    return reprocess(config.load(config_path), holon, medium, reason, redundancy=redundancy,
                     redundancy_reason=redundancy_reason)


@main.command("recover", help="finish or roll back an interrupted transaction")
@click.argument("holon")
@click.pass_obj
def recover_cmd(config_path: str | None, holon: str) -> int:
    from .archive.transaction import recover
    from .system.macos import FileLock

    cfg = config.load(config_path)
    holon = normalize_holon_id(holon)
    pd = cfg.archive_root / holon
    if not pd.is_dir():
        click.echo(f"no product directory {pd}")
        return 0
    with FileLock(pd / "product.lock"):
        actions = recover(pd, cfg.work_root)
    if not actions:
        click.echo(f"{holon}: nothing to recover")
    return 0


if __name__ == "__main__":
    main()
