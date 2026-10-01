# coa — copy of a disc

`coa` archives optical discs bit-exactly with [redumper](https://github.com/superg/redumper) on macOS: it runs
redumper's stages one by one, records everything produced while the disc is in the drive, verifies the dump,
and publishes a self-describing, checksummed archive that standard tools (`shasum`, `flac`, `xz`, `zstd`, `jq`,
`par2`) can rebuild without `coa`.

The specification (requirements, archive format, v1 scope) lives in Notion: *Disc Archiver Specification*.

## Requirements

- macOS, Python 3.14, [uv](https://docs.astral.sh/uv/)
- redumper build `b753+d8fix` (the only allow-listed build), `flac`, `xz`, `zstd`, `par2` on `PATH`
- a configured drive (default: PLEXTOR PX-W5224A, firmware 1.04)

## Setup

```bash
uv sync
uv run coa init --archive-root /Volumes/Archive/discs --work-root /Volumes/Work/coa
uv run coa drive-test          # with a known error-free mixed-mode CD inserted
```

`coa init` writes `~/.config/copyofa/config.toml` (override with `--config` or `$COA_CONFIG`) and the shared
medium-prefix vocabulary `<archive_root>/media-prefixes.toml`. The work root must not be inside `~/Desktop`,
`~/Documents` or `~/Downloads`. Approve an acquisition profile by adding it to `[profiles] approved`; until then
every archive is provisional.

## Archiving a disc

```bash
uv run coa archive T02PQ CD1 --media-list CD1        # first medium of a product declares the media list
uv run coa accept T02PQ CD1 provisional-capture --reason "the pressed audio CD profile is still a draft"
uv run coa archive T02PQ CD1                         # resumes where it stopped
```

Decisions are stop-and-resume: a blocking issue ends the run with `OUTCOME: FAIL` and the exact commands that
resolve it (`coa accept …`, `coa refine …`, `coa discard … --yes`); rerun `coa archive` afterwards. Interrupted
stages are repeated under a new stage number. Once acquisition is complete the drive is no longer needed.

| Command | Purpose |
|---|---|
| `coa archive H M [--media-list L] [--drive N] [--wait S] [--redundancy on\|off --redundancy-reason T]` | acquire, verify, publish (resumable) |
| `coa refine H M --retries N [--speed X] [--refine-subchannel]` | another refine pass after acquisition, then a new verification |
| `coa accept H M CODE --reason T` | accept an open acceptable blocking issue |
| `coa discard H M [--yes]` | delete the work directory (asks for confirmation on a terminal unless `--yes`) |
| `coa drive-test [--drive N]` | run and cache `redumper drive::test` |
| `coa status [H [M]]` | work in progress, product media table, completeness |
| `coa verify H [M] [--deep] [--repair]` | fast or deep verification; PAR2 repair through a reseal |
| `coa amend H --media-list L --reason T` | change a product's media list (moves files when the layout changes) |
| `coa reprocess H M --reason T [--redundancy on\|off --redundancy-reason T]` | re-derive metadata from the archived raw files and reseal |
| `coa recover H` | finish or roll back an interrupted publication/reseal/amendment |

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

`coa` never writes to a medium: it refuses every macOS mount of the configured drives while it controls them
(DiskArbitration approval callback), rejects blank media right after `drutil status`, only invokes allow-listed
redumper commands and options (`split` always with `--leave-unchanged`), and never runs `diskutil`, `hdiutil`,
or `drutil` verbs other than `status`/`list`.

## Development

Requires [just](https://github.com/casey/just) besides uv; `just --list` shows every recipe.

```bash
just setup                       # install all dependency groups and the pre-commit hooks
just test                        # pytest with coverage; pilot tests read $COA_PILOT_DUMPS (default ~/Dumps) and skip when absent
just test -m "not macos_integration"
just qa                          # dead code, unused deps, formatting, lint (ruff), strict types (mypy)
```

The archive tests need `flac`, `jq`, `par2`, `xz` and `zstd`. CI (`.github/workflows/ci.yml`) runs the QA checks
and the suite on macOS with Python 3.14, both with the locked dependencies and with the oldest versions
`pyproject.toml` allows. `coa` supports Python 3.14 only; `requires-python` refuses other interpreters.
Conventions for contributors and coding agents are in [AGENTS.md](AGENTS.md).
