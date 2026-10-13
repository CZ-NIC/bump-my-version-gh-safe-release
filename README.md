# bump-my-version-gh-safe-release

Release only what passed the GitHub CI.

A thin layer over [bump-my-version](https://github.com/callowayproject/bump-my-version), which does all the versioning work: editing the files, committing, tagging. This tool only adds what bump-my-version deliberately leaves out, because it is tied to git hosting:

1. **Preflight:** the working tree is clean, you are on `main`, and `HEAD` equals `origin/main`, so you release exactly what is pushed.
2. **Wait for the CI:** every GitHub Actions run of that commit must succeed (runs still in progress are waited for, a dropped connection is retried, not taken for a failure). Nothing is tagged otherwise, so no version goes out to be followed by a "CI failed" e-mail and no dangling tag remains.
3. **Bump or tag:** `bump-my-version bump PART --commit --tag`, or just tag a version already committed.
4. **Push** the commit and the tag atomically: both or neither.

*Not affiliated with bump-my-version or its authors.*

## Install

`pip install bump-my-version-gh-safe-release` into any environment, or let a Makefile install it once per machine (see [Through make](#through-make)). Needs `git` and an authenticated [`gh`](https://cli.github.com/).

## Use

Configure bump-my-version as usual (`[tool.bumpversion]` in `pyproject.toml` or `.bumpversion.toml`). Then either:

```bash
gh-safe-release patch    # (or minor, major…) bump, commit, tag, push the commit and the tag
gh-safe-release          # the bump is committed and pushed already: check the files hold the version, tag, push the tag
```

| Option | |
|---|---|
| `-w, --workflow NAME` | Wait for this workflow (name or file name) only, may be repeated. Default: all runs of the commit pushed to the branch. |
| `--no-wait-ci` | Skip the CI (ex: a repo without one, or hosted elsewhere). |
| `--remote`, `--branch` | Default: `origin`, `main`. |
| `-n, --dry-run` | Check everything, change nothing. |

Without the part, every file configured in bump-my-version must contain its `replace` text rendered with the current version (other placeholders, like a date, match anything), and the tag must not exist yet. For example with

```toml
[tool.bumpversion]
current_version = "1.2.0"

[[tool.bumpversion.files]]
filename = "CHANGELOG.md"
search = "## Unreleased"
replace = "## {new_version} ({now:%Y-%m-%d})"
```

the `CHANGELOG.md` must contain a `## 1.2.0 (…)` heading.

## Through make

Keep `make release` as the entry point: the Makefile only installs the tool once per machine (one venv shared by all your projects, not one per project) and calls it. Project-specific steps go there too, as prerequisites of `release`.

```make
# make release PART=patch|minor|major   bump, commit, tag, push both (after CI passed on main)
# make release                          tag a bump committed already, push the tag
# Upgrade: $(TOOL)/bin/pip install -U bump-my-version-gh-safe-release
TOOL := $(HOME)/.local/share/gh-safe-release
.PHONY: release

release: $(TOOL)/bin/gh-safe-release
	$< $(PART)

$(TOOL)/bin/gh-safe-release:
	python3 -m venv $(TOOL) && $(TOOL)/bin/pip install -q bump-my-version-gh-safe-release
```

A dry run: `make release PART="patch -n"`.

## A changelog-driven release

The pattern this tool is built for (see its own `pyproject.toml`):

* Write the changes under a `## Unreleased` heading in `CHANGELOG.md`, the bump turns it into `## X.Y.Z (YYYY-MM-DD)`. List the `CHANGELOG.md` first in `[[tool.bumpversion.files]]`, so that a missing heading fails before anything is written.
* `gh-safe-release patch`.
* The tag push triggers a CI workflow that runs the tests again, then publishes (PyPI, a GitHub release with the CHANGELOG section as notes). See [`.github/workflows/publish.yml`](.github/workflows/publish.yml).
