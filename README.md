# CopyOfA

> A copy of a disc, bit for bit.

CopyOfA archives optical discs bit-exactly with [redumper](https://github.com/superg/redumper) on macOS: it runs
redumper's stages one by one, records everything produced while the disc is in the drive, verifies the dump,
and publishes a self-describing, checksummed archive that standard tools (`shasum`, `flac`, `xz`, `zstd`, `jq`,
`par2`) can rebuild without `copyofa`.

## Requirements

- macOS, Python 3.14, [uv](https://docs.astral.sh/uv/)
- redumper build `b753+d8fix` (the only allow-listed build), `flac`, `xz`, `zstd`, `par2` on `PATH`
- a configured drive (default: PLEXTOR PX-W5224A, firmware 1.04)

## Setup

```bash
uv sync
uv run copyofa init --archive-root /Volumes/Archive/discs --work-root /Volumes/Work/copyofa
```

`copyofa init` writes `~/.config/copyofa/config.toml` (override with `--config` or `$COPYOFA_CONFIG`) and the shared
medium-prefix vocabulary `<archive_root>/media-prefixes.toml`. The work root must not be inside `~/Desktop`,
`~/Documents` or `~/Downloads`. Approve an acquisition profile by adding it to `[profiles] approved`; until then
every archive is provisional.

## Archiving a disc

```bash
uv run copyofa archive T02PQ CD1 --media-list CD1        # first medium of a product declares the media list
uv run copyofa accept T02PQ CD1 provisional-capture --reason "the pressed audio CD profile is still a draft"
uv run copyofa archive T02PQ CD1                         # resumes where it stopped
```

Decisions are stop-and-resume: a blocking issue ends the run with `OUTCOME: FAIL` and the exact commands that
resolve it (`copyofa accept …`, `copyofa refine …`, `copyofa discard … --yes`); rerun `copyofa archive` afterwards. Interrupted
stages are repeated under a new stage number. Once acquisition is complete the drive is no longer needed.

| Command | Purpose |
|---|---|
| `copyofa archive H M [--media-list L] [--drive N] [--wait S] [--redundancy on\|off --redundancy-reason T]` | acquire, verify, publish (resumable) |
| `copyofa refine H M --retries N [--speed X] [--refine-subchannel]` | another refine pass after acquisition, then a new verification |
| `copyofa accept H M CODE --reason T` | accept an open acceptable blocking issue |
| `copyofa discard H M [--yes]` | delete the work directory (asks for confirmation on a terminal unless `--yes`) |
| `copyofa drive-test [--drive N]` | run and cache `redumper drive::test` |
| `copyofa status [H [M]]` | work in progress, product media table, completeness |
| `copyofa verify H [M] [--deep] [--repair]` | fast or deep verification; PAR2 repair through a reseal |
| `copyofa amend H --media-list L --reason T` | change a product's media list (moves files when the layout changes) |
| `copyofa reprocess H M --reason T [--redundancy on\|off --redundancy-reason T]` | re-derive metadata from the archived raw files and reseal |
| `copyofa recover H` | finish or roll back an interrupted publication/reseal/amendment |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | PASS / success |
| 10 | PASS WITH ACCEPTED ISSUES |
| 20 | FAIL: blocking issues await a decision or a retry (resumable) |
| 21 | medium rejected (blank, wrong prefix, unsupported, already archived, …) |
| 22 | environment or preflight failure, lock busy |
| 30 | integrity damage found or unrecoverable |
| 31 | uncommitted transaction or leftover transaction directories |
| 2 | usage error / invalid input |
| 70 | internal error |

## Safety

`copyofa` never writes to a medium: it refuses every macOS mount of the configured drives while it controls them
(DiskArbitration approval callback), rejects blank media right after `drutil status`, only invokes allow-listed
redumper commands and options (`split` always with `--leave-unchanged`), and never runs `diskutil`, `hdiutil`,
or `drutil` verbs other than `status`/`list`.

## Development

Requires [just](https://github.com/casey/just) besides uv; `just --list` shows every recipe.

```bash
just setup                       # install all dependency groups and the pre-commit hooks
just test                        # pytest with coverage; pilot tests read $COPYOFA_PILOT_DUMPS (default ~/Dumps) and skip when absent
just test -m "not macos_integration"
just qa                          # dead code, unused deps, formatting, lint (ruff), strict types (mypy)
```

The archive tests need `flac`, `jq`, `par2`, `xz` and `zstd`. CI (`.github/workflows/ci.yml`) runs the QA checks
and the suite on macOS with Python 3.14, both with the locked dependencies and with the oldest versions
`pyproject.toml` allows. `copyofa` supports Python 3.14 only; `requires-python` refuses other interpreters.
Conventions for contributors and coding agents are in [AGENTS.md](AGENTS.md).
