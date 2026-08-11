# Contributing to RSBC DMER Automation

## Branching

- `main` is protected and always deployable.
- Feature work happens on `feature/<ticket>-<short-desc>` branches.
- Open a pull request into `main`; at least one approving review from CODEOWNERS is required.

## Commit conventions

Use [Conventional Commits](https://www.conventionalcommits.org/) (e.g. `feat(intake-processor): add batch poller retry`).
Allowed types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`.
This is enforced automatically — see "Pre-commit hooks" below.

## Pre-commit hooks

This repo uses [pre-commit](https://pre-commit.com/) to catch issues before they're committed,
not after they hit a PR. One-time setup:

```bash
pip install -r requirements-dev.txt --break-system-packages
pre-commit install                      # runs on every `git commit`
pre-commit install --hook-type commit-msg   # validates your commit message
```

What runs on every commit (see `.pre-commit-config.yaml` for the full list):


| Hook                                                            | What it does                                                               | Auto-fixes?                                   |
| --------------------------------------------------------------- | -------------------------------------------------------------------------- | --------------------------------------------- |
| `black`                                                         | Formats staged Python                                                      | Yes — re-stage and commit again               |
| `ruff`                                                          | Lints staged Python                                                        | Fixable issues, yes; the rest you fix by hand |
| `detect-secrets`                                                | Blocks commits containing likely secrets/keys, against `.secrets.baseline` | No                                            |
| `bicep-lint`                                                    | Lints staged `.bicep` files (`az bicep lint`)                              | No                                            |
| `conventional-pre-commit`                                       | Validates your commit message format                                       | No                                            |
| `end-of-file-fixer`, `trailing-whitespace`, `mixed-line-ending` | General file hygiene                                                       | Yes                                           |
| `check-yaml`, `check-json`, `check-toml`, `check-ast`           | Catches malformed config/code before it merges                             | No                                            |


If a hook modifies files (black, ruff, or the hygiene hooks), the commit is stopped, the fix is
already applied to your working tree — `git add -u` and commit again. If a hook reports something
it *can't* fix (a real secret, invalid YAML, a bad commit message), fix it yourself and retry.

The same hooks run again in CI (`lint.yml`) against the full repo, so nothing that would fail
locally can pass a PR by skipping `--no-verify`.

## Before opening a PR

1. Run the service's unit tests locally (`docs/development/local-development.md`).
2. Let pre-commit run `black` / `ruff` for any Python code you touched (see above); run `mypy`
  manually for now (not yet wired into pre-commit — see `lint.yml`).
3. Let pre-commit's `bicep-lint` hook run, and `az bicep build` for anything deeper.
4. Update the relevant `docs/services/*.md` or `docs/contracts/`* file if behavior or contracts changed.

## Code review standards

See `docs/standards/bc-gov-alignment.md` and `docs/development/coding-standards.md`.
