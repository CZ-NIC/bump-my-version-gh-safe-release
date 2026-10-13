import json
import sys
import subprocess
from pathlib import Path

import pytest

import gh_safe_release
from gh_safe_release import main, replace_pattern

PYPROJECT = '''[project]
name = "demo"
version = "1.0.0"

[tool.bumpversion]
current_version = "1.0.0"
tag_name = "v{new_version}"
message = "chore(release): {new_version}"

[[tool.bumpversion.files]]
filename = "CHANGELOG.md"
search = "## Unreleased"
replace = "## {new_version} ({now:%Y-%m-%d})"

[[tool.bumpversion.files]]
filename = "pyproject.toml"
search = '^version = "{current_version}"'
replace = 'version = "{new_version}"'
regex = true
'''


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A clone of a bare origin, holding a released 1.0.0 with new changes under the Unreleased heading."""
    git("init", "-q", "--bare", "-b", "main", str(tmp_path / "origin.git"))
    work = tmp_path / "work"
    git("clone", "-q", str(tmp_path / "origin.git"), str(work))
    monkeypatch.chdir(work)
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    Path("pyproject.toml").write_text(PYPROJECT)
    Path("CHANGELOG.md").write_text("# CHANGELOG\n\n## Unreleased\n- feat\n\n## 1.0.0 (2026-01-01)\n- first\n")
    git("add", ".")
    git("commit", "-qm", "init")
    git("tag", "-a", "v1.0.0", "-m", "1.0.0")
    git("push", "-q", "--follow-tags", "origin", "main")
    monkeypatch.setattr(gh_safe_release, "POLL", 0)
    return work


@pytest.fixture
def ci(monkeypatch):
    """Fake `gh run list`: set `ci.runs` to the runs it returns (None = GitHub unreachable)."""
    class CI:
        runs: list | None = [{"databaseId": 1, "workflowName": "tests", "status": "completed",
                              "conclusion": "success", "url": "https://ci/1"}]
        calls = 0
    real = subprocess.run

    def fake(cmd, *args, **kwargs):
        if cmd[:3] == ["gh", "run", "list"]:
            CI.calls += 1
            runs = CI.runs(CI.calls) if callable(CI.runs) else CI.runs
            if runs is None:
                return subprocess.CompletedProcess(cmd, 1, "", "connection timed out")
            return subprocess.CompletedProcess(cmd, 0, json.dumps(runs), "")
        return real(cmd, *args, **kwargs)
    monkeypatch.setattr(subprocess, "run", fake)
    return CI


def new_remote_tags():
    """Tags on the origin besides the v1.0.0 the repo starts with."""
    return set(git("ls-remote", "--tags", "--refs", "origin").replace("refs/tags/", "").split()[1::2]) - {"v1.0.0"}


def test_bump(repo, ci):
    assert main(["patch"]) == 0
    assert 'version = "1.0.1"' in Path("pyproject.toml").read_text()
    assert "## 1.0.1 (" in Path("CHANGELOG.md").read_text()
    assert git("log", "-1", "--format=%s") == "chore(release): 1.0.1"
    assert new_remote_tags() == {"v1.0.1"}
    assert git("rev-parse", "HEAD") == git("rev-parse", "origin/main")


def test_tag_only(repo, ci, capsys):
    assert main([]) == 1  # the version has not been bumped yet
    assert "Tag v1.0.0 exists already" in capsys.readouterr().err

    Path("pyproject.toml").write_text(PYPROJECT.replace('"1.0.0"', '"1.1.0"'))  # bumped by hand, CHANGELOG forgotten
    git("commit", "-qam", "release")
    git("push", "-q", "origin", "main")
    assert main([]) == 1
    assert "CHANGELOG.md does not contain the version 1.1.0" in capsys.readouterr().err
    git("reset", "-q", "--hard", "HEAD~")
    git("push", "-qf", "origin", "main")

    subprocess.run([sys.executable, "-m", "bumpversion", "bump", "minor", "--no-commit", "--no-tag"], check=True)
    git("commit", "-qam", "release")
    git("push", "-q", "origin", "main")
    assert main([]) == 0
    assert new_remote_tags() == {"v1.1.0"}

    assert main([]) == 1
    assert "Tag v1.1.0 exists already" in capsys.readouterr().err


def test_ci_failed(repo, ci, capsys):
    ci.runs = [{"databaseId": 1, "workflowName": "tests", "status": "completed", "conclusion": "failure",
                "url": "https://ci/1"}]
    assert main(["patch"]) == 1
    assert "CI failed, nothing released: tests (failure) https://ci/1" in capsys.readouterr().err
    assert not new_remote_tags()
    assert 'version = "1.0.0"' in Path("pyproject.toml").read_text()


def test_ci_waits_through_network_errors(repo, ci):
    """A dropped connection is not a failed CI (it used to be with `gh run watch --exit-status`)."""
    run = {"databaseId": 1, "workflowName": "tests", "url": "https://ci/1"}
    ci.runs = lambda n: ([] if n == 1 else None if n == 2 else [run | {"status": "in_progress", "conclusion": ""}]
                         if n == 3 else [run | {"status": "completed", "conclusion": "success"}])
    assert main(["patch"]) == 0
    assert ci.calls >= 4


def test_no_ci_run(repo, ci, monkeypatch, capsys):
    ci.runs = []
    monkeypatch.setattr(gh_safe_release, "APPEAR", 0)
    assert main(["patch"]) == 1
    assert "No CI run" in capsys.readouterr().err
    assert main(["patch", "--no-wait-ci"]) == 0


def test_preflight(repo, ci, capsys):
    Path("pyproject.toml").write_text(PYPROJECT + "\n")
    assert main(["patch"]) == 1
    assert "not clean" in capsys.readouterr().err

    git("commit", "-qam", "unpushed")
    assert main(["patch"]) == 1
    assert "HEAD differs from origin/main" in capsys.readouterr().err
    assert not new_remote_tags()


def test_dry_run(repo, ci):
    assert main(["patch", "--dry-run"]) == 0
    assert 'version = "1.0.0"' in Path("pyproject.toml").read_text()
    assert not new_remote_tags()


@pytest.mark.parametrize("replace, text, found", [
    ("## {new_version} ({now:%Y-%m-%d})", "## 2.0.0 (2026-10-13)", True),
    ("## {new_version} ({now:%Y-%m-%d})", "## 2.0.1 (2026-10-13)", False),
    ("## {new_version} ({now:%Y-%m-%d})", "## Unreleased", False),
    ('version = "{new_version}"', 'version = "2.0.0"', True),
    ("{{{new_version}}}", "{2.0.0}", True),
])
def test_replace_pattern(replace, text, found):
    assert bool(replace_pattern(replace, "2.0.0").search(text)) is found
