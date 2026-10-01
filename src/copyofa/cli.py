"""Command-line interface `coa`."""

import argparse
import sys
import traceback
from pathlib import Path

from . import __version__, config
from .acquisition import acquire
from .acquisition.workstate import WorkPaths, load_state
from .archive import product as product_mod
from .errors import EXIT_ACCEPTED, EXIT_INTERNAL, EXIT_PASS, CoaError
from .ids import normalize_holon_id, parse_medium_id
from .issues import ACCEPTED, FAIL, PASS, Issue, open_blocking, render


def _outcome_line(code: int) -> str:
    return f"OUTCOME: {PASS if code == EXIT_PASS else ACCEPTED if code == EXIT_ACCEPTED else FAIL}"


def cmd_init(args, cfg_path: Path) -> int:
    paths = config.init(cfg_path, Path(args.archive_root), Path(args.work_root))
    print(f"config: {paths[0]}\narchive root: {paths[1]}\nwork root: {paths[2]}\nprefix vocabulary: {paths[3]}")
    return 0


def cmd_status(cfg, args) -> int:
    if not args.holon:
        rows = []
        if cfg.work_root.exists():
            for d in sorted(cfg.work_root.iterdir()):
                if d.is_dir() and (d / "state.json").exists():
                    holon, _, medium = d.name.partition("-")
                    state = load_state(WorkPaths(cfg.work_root, holon, medium))
                    issues = [Issue.from_dict(i) for i in state["issues"] + state["pre_dump_issues"]]
                    codes = ", ".join(i.code for i in open_blocking(issues, state["acceptances"])) or "-"
                    rows.append(f"{d.name}  {state['phase']}  open: {codes}")
        print("\n".join(rows) if rows else "no work in progress")
        return 0
    from .archive.transaction import journals

    holon = normalize_holon_id(args.holon)
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
            print(f"{entry['medium_id']:8} {entry['status']}{extra}")
        print(product_mod.completeness_line(doc))
        permanent, total, _ = product_mod.completeness(doc)
        if permanent < total:
            print(f"ISSUE incomplete-product: {total - permanent} optical medium/media not permanently archived")
        for journal in journals(pd):
            print(f"UNCOMMITTED {journal.relative_to(pd)}: run coa recover {holon}")
        for path in pd.iterdir():
            if path.name.startswith((".coa-staging-", ".coa-backup-")):
                print(f"UNCOMMITTED leftover {path.name}: run coa recover {holon}")
    else:
        print(f"{holon}: no product manifest")
    media = [str(parse_medium_id(args.medium))] if args.medium else None
    for d in sorted(cfg.work_root.glob(f"{holon}-*")) if cfg.work_root.exists() else []:
        medium = d.name.partition("-")[2]
        if media and medium not in media:
            continue
        state = load_state(WorkPaths(cfg.work_root, holon, medium))
        if state is None:
            continue
        print(f"work {d.name}: {state['phase']}")
        if not args.medium:
            continue
        for s in state["stages"]:
            label = f" ({s['label']})" if s["label"] else ""
            nn = f"{s['nn']:02d}" if s["nn"] is not None else "--"
            print(f"  {nn} {s['name']}{label}: exit {s['exit_code']}{' interrupted' if s['interrupted'] else ''}")
        for skipped in state["skipped_stages"]:
            print(f"  skipped {skipped['command']} ({skipped['label']}): {skipped['reason']}")
        issues = [Issue.from_dict(i) for i in state["issues"] + state["pre_dump_issues"]]
        for issue in open_blocking(issues, state["acceptances"]):
            print(render(issue))
        for acceptance in state["acceptances"]:
            print(f"  accepted {acceptance['code']} at {acceptance['accepted_at']}: {acceptance['reason']}")
    return 0


def cmd_recover(cfg, args) -> int:
    from .archive.transaction import recover
    from .system.macos import FileLock

    holon = normalize_holon_id(args.holon)
    pd = cfg.archive_root / holon
    if not pd.is_dir():
        print(f"no product directory {pd}")
        return 0
    with FileLock(pd / "product.lock"):
        actions = recover(pd, cfg.work_root)
    if not actions:
        print(f"{holon}: nothing to recover")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coa", description="Archive optical discs bit-exactly with redumper")
    parser.add_argument("--config", help="config file (default $COA_CONFIG or ~/.config/copyofa/config.toml)")
    parser.add_argument("--version", action="version", version=f"coa {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="write the config file and create the roots")
    p.add_argument("--archive-root", required=True)
    p.add_argument("--work-root", required=True)

    p = sub.add_parser("archive", help="acquire, verify and publish a medium (resumable)")
    p.add_argument("holon")
    p.add_argument("medium")
    p.add_argument("--media-list", help="declared media list of the product (first medium only)")
    p.add_argument("--drive", help="configured drive name")
    p.add_argument("--wait", type=float, default=300, help="seconds to wait for a medium (default 300)")
    p.add_argument("--redundancy", choices=("on", "off"))
    p.add_argument("--redundancy-reason")

    p = sub.add_parser("refine", help="run another refine pass after acquisition, then verify again")
    p.add_argument("holon")
    p.add_argument("medium")
    p.add_argument("--retries", type=int, required=True)
    p.add_argument("--speed", type=float)
    p.add_argument("--refine-subchannel", action="store_true")

    p = sub.add_parser("accept", help="accept an open blocking issue")
    p.add_argument("holon")
    p.add_argument("medium")
    p.add_argument("code")
    p.add_argument("--reason", required=True)

    p = sub.add_parser("discard", help="delete the work directory of a medium")
    p.add_argument("holon")
    p.add_argument("medium")
    p.add_argument("--yes", action="store_true", required=True)

    p = sub.add_parser("drive-test", help="run and cache redumper drive::test")
    p.add_argument("--drive")
    p.add_argument("--wait", type=float, default=300)

    p = sub.add_parser("status", help="show work in progress or a product")
    p.add_argument("holon", nargs="?")
    p.add_argument("medium", nargs="?")

    p = sub.add_parser("verify", help="verify a product (fast, or --deep); --repair uses PAR2")
    p.add_argument("holon")
    p.add_argument("medium", nargs="?")
    p.add_argument("--deep", action="store_true")
    p.add_argument("--repair", action="store_true")

    p = sub.add_parser("amend", help="change the media list of a product")
    p.add_argument("holon")
    p.add_argument("--media-list", required=True)
    p.add_argument("--reason", required=True)

    p = sub.add_parser("reprocess", help="re-derive the metadata from the archived raw files and reseal")
    p.add_argument("holon")
    p.add_argument("medium")
    p.add_argument("--reason", required=True)
    p.add_argument("--redundancy", choices=("on", "off"))
    p.add_argument("--redundancy-reason")

    p = sub.add_parser("recover", help="finish or roll back an interrupted transaction")
    p.add_argument("holon")
    return parser


def dispatch(args) -> int:
    if args.command == "init":
        return cmd_init(args, config.resolve_path(args.config))
    cfg = config.load(args.config)
    match args.command:
        case "archive":
            return acquire.archive(cfg, args.holon, args.medium, media_list=args.media_list, drive_name=args.drive,
                                   wait=args.wait, redundancy=args.redundancy, redundancy_reason=args.redundancy_reason)
        case "refine":
            return acquire.refine(cfg, args.holon, args.medium, retries=args.retries, speed=args.speed,
                                  refine_subchannel=args.refine_subchannel)
        case "accept":
            return acquire.accept(cfg, args.holon, args.medium, args.code, args.reason)
        case "discard":
            return acquire.discard(cfg, args.holon, args.medium)
        case "drive-test":
            return acquire.drive_test(cfg, args.drive, args.wait)
        case "status":
            return cmd_status(cfg, args)
        case "verify":
            from .archive.verify import verify_command

            return verify_command(cfg, args.holon, args.medium, deep=args.deep, repair=args.repair)
        case "amend":
            return product_mod.amend(cfg, args.holon, args.media_list, args.reason)
        case "reprocess":
            from .archive.reprocess import reprocess

            return reprocess(cfg, args.holon, args.medium, args.reason, redundancy=args.redundancy,
                             redundancy_reason=args.redundancy_reason)
        case "recover":
            return cmd_recover(cfg, args)
    raise AssertionError(args.command)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    with_outcome = args.command in ("archive", "refine")
    try:
        code = dispatch(args)
    except CoaError as exc:
        label = f" [{exc.code}]" if exc.code else ""
        print(f"coa: {type(exc).__name__}{label}: {exc}", file=sys.stderr)
        code = exc.exit_code
    except KeyboardInterrupt:
        print("coa: interrupted", file=sys.stderr)
        code = 130
    except Exception as exc:  # noqa: BLE001 - internal error
        print(f"coa: internal error: {exc}", file=sys.stderr)
        traceback.print_exc()
        code = EXIT_INTERNAL
    if with_outcome:
        print(_outcome_line(code), flush=True)
    sys.exit(code)


if __name__ == "__main__":
    main()
