# Instructions for coding agents

## Commands

The task runner is [just](https://github.com/casey/just); every recipe runs
through `uv run`, so the tools come from the versions pinned in `uv.lock`.

```bash
just setup              # Install all dependency groups and the pre-commit hooks
just test               # pytest with coverage; extra arguments go to pytest
just qa                 # dead-code, deps-unused, format-check, lint, type-check
just format             # ruff format + pyproject-fmt
just lint-fix           # ruff check --fix
just type-check         # strict mypy over src and tests
just dead-code          # vulture over src and tests
just deps-unused        # deptry src
just deps-update        # Upgrade uv.lock and sync
just pre-commit         # Run every pre-commit hook on all files
```

Run a single test with `just test tests/test_cli.py -k discard`. Before finishing
a change, run `just qa` and `just test`; CI runs the same checks on macOS.

## Testing

- `pilot` tests read real dumps from `$COA_PILOT_DUMPS` (default `~/Dumps`) and
  skip when they are absent.
- `macos_integration` tests attach a temporary disk image with `hdiutil`;
  deselect them with `-m "not macos_integration"`.
- The archive tests need `flac`, `jq`, `par2`, `xz` and `zstd` on `PATH`.
- Files under `tests/fixtures/` are the tools' own output (redumper logs, drutil
  status). Never edit them; the pre-commit fixers skip them.
- Run the suite outside `~/Desktop`, `~/Documents` and `~/Downloads`: `coa`
  refuses work roots there.

## Language

Write US English in everything: code, comments, docstrings, messages, tests,
documentation and commit messages (`-ize`, `-or`, `-er`, `canceled`, `catalog`).
Optical media are "discs"; "disk" stays correct for other storage and in macOS
names such as `diskutil`.

## Conventions

- Lint and format with ruff at a line length of 88. Suppress a finding only with
  `# noqa: CODE - reason`; repository-wide ignores live in `pyproject.toml`, each
  with its reason.
- Strict mypy covers `src` and `tests`. `# type: ignore[code]` only at untyped
  boundaries (PyObjC), with a reason.
- Declare every third-party module the code imports as a direct dependency;
  `deptry` enforces it. Add dependencies with `uv add`, never by editing
  `pyproject.toml` by hand.
- Workarounds that are knowingly less than ideal carry a `KLUDGE:` comment that
  states what is wrong and what removes it. Grep for `KLUDGE` before changing
  such code, and do not add one without a removal condition.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):
  `<type>: <description>` with `feat`, `fix`, `refactor`, `chore`, `docs`,
  `test` or `ci`.
- Run `git` commands directly, never with `git -C`.
