"""Verification run: split/hash/info through links to the read-only raw files."""

import os
import shutil

from ..redumper import console
from ..redumper.commands import build_argv
from ..system.procs import run_recorded
from .workstate import next_nn, save_state, snapshot

REFUSAL = "error: data errors detected, unable to continue"


def run_verification(run) -> None:
    """Run split (and hash/info if split succeeded) as new numbered stages in a temporary directory."""
    state, paths = run.state, run.paths
    nn = next_nn(state)
    vdir = paths.verify_dir(nn)
    if vdir.exists():
        shutil.rmtree(vdir)
    vdir.mkdir()
    try:
        for path in sorted(paths.dump.iterdir()):
            if path.is_file() and path.name != f"{run.image}.log":
                os.symlink(path.resolve(), vdir / path.name)
        split_options = ["--leave-unchanged"] + (["--force-split"] if state["force_split"] else [])
        record = {"split_nn": nn, "hash_nn": None, "info_nn": None, "log_record": None,
                  "force_split": state["force_split"]}
        stage_nn = nn
        retry_needed = False
        for command, options in (("split", split_options), ("hash", []), ("info", [])):
            argv = build_argv(run.redumper, command, image_path=vdir, image_name=run.image, options=options)
            record_path = paths.stage_record(stage_nn, command)
            print(f"--- stage {stage_nn:02d}: {command}: {' '.join(argv)}", flush=True)
            result = run_recorded(argv, record_path, cwd=vdir)
            state["stages"].append({
                "nn": stage_nn,
                "name": command,
                "kind": "verification",
                "label": None,
                "argv": result.argv,
                "command_line": result.command_line,
                "started_at": result.started_at,
                "ended_at": result.ended_at,
                "exit_code": result.exit_code,
                "interrupted": result.interrupted,
                "record": record_path.name,
                "cached_at": None,
                "counts_after": None,
                "reported": None,
            })
            if command != "split":
                record[f"{command}_nn"] = stage_nn
            save_state(paths, state)
            if result.exit_code != 0 or result.interrupted:
                refused = command == "split" and not state["force_split"] and not result.interrupted and (
                    REFUSAL in console.clean(record_path.read_bytes())
                )
                retry_needed = not refused  # a refused split waits for a decision, any other failure is rerun
                break
            stage_nn += 1
        log = vdir / f"{run.image}.log"
        if log.exists() and not log.is_symlink():
            target = paths.verify_log(nn)
            shutil.copyfile(log, target)
            target.chmod(0o444)
            record["log_record"] = target.name
        current = snapshot(paths.dump)
        unchanged = current == state["raw_snapshot"]
        changed = sorted(
            name for name in set(current) | set(state["raw_snapshot"])
            if current.get(name) != state["raw_snapshot"].get(name)
        )
        state["acquisition_checks"].append({
            "id": f"raw-unchanged-{nn:02d}",
            "origin": "acquisition",
            "status": "pass" if unchanged else "fail",
            "values": {"changed": changed},
            "note": "raw files re-hashed after the verification run equal the snapshot taken after acquisition",
        })
    finally:
        shutil.rmtree(vdir, ignore_errors=True)
    state["verifications"].append(record)
    state["verification_needed"] = retry_needed
    save_state(paths, state)
