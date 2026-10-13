"""Release only what passed the GitHub CI: a thin layer over bump-my-version."""

import json
import re
import subprocess
import sys
import time
from argparse import ArgumentParser
from pathlib import Path

POLL = 10  # seconds between CI status checks
APPEAR = 60  # seconds to wait for GitHub to start the runs of a just-pushed commit


class ReleaseError(Exception):
    pass


def say(msg: str) -> None:
    print(f"gh-safe-release: {msg}", file=sys.stderr, flush=True)


def run(*cmd: str, check=True) -> str:
    """Run the command and return its stripped stdout."""
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode:
        raise ReleaseError(f"{' '.join(cmd)} failed:\n{p.stderr.strip()}")
    return p.stdout.strip()


def bmv(*args: str, check=True) -> str:
    # stderr dropped: before tagging, bump-my-version warns that the version does not match the last tag
    return run(sys.executable, "-m", "bumpversion", *args, check=check)


def bmv_config() -> dict:
    return json.loads(bmv("show", "--format", "json", "current_version", "tag_name", "tag_message", "files"))


def render(template: str, version: str) -> str:
    """Render a tag name/message, where the only placeholder may be the version."""
    try:
        return template.format(new_version=version, current_version=version)
    except (KeyError, IndexError, ValueError) as e:
        raise ReleaseError(f"Cannot render {template!r}, only {{new_version}} is supported: {e!r}")


def replace_pattern(replace: str, version: str) -> re.Pattern:
    """What the file contains after the bump to the version: the `replace` text, other placeholders being anything."""
    parts = re.split(r"(\{\{|\}\}|\{[^{}]*\})", replace)
    rx = []
    for part in parts:
        if part in ("{{", "}}"):
            rx.append(re.escape(part[0]))
        elif part.startswith("{") and part.endswith("}"):
            rx.append(re.escape(version) if part[1:-1] == "new_version" else ".*?")
        else:
            rx.append(re.escape(part))
    return re.compile("".join(rx))


def check_files(config: dict) -> None:
    """Every configured file holds the current version, as a bump would have written it."""
    version = config["current_version"]
    for file in config["files"]:
        paths = [Path(file["filename"])] if file["filename"] else sorted(Path().glob(file["glob"]))
        pattern = replace_pattern(file["replace"], version)
        for path in paths:
            if not path.exists():
                if file["ignore_missing_file"]:
                    continue
                raise ReleaseError(f"{path} not found")
            if not pattern.search(path.read_text(encoding="utf-8")) and not file["ignore_missing_version"]:
                raise ReleaseError(f"{path} does not contain the version {version}: no match for {pattern.pattern!r}."
                                   " Has the version been bumped?")


def preflight(remote: str, branch: str) -> str:
    """Release exactly what is on the remote branch. Return the HEAD hash."""
    if run("git", "status", "--porcelain", "--untracked-files=no"):
        raise ReleaseError("The working tree is not clean.")
    if (current := run("git", "branch", "--show-current")) != branch:
        raise ReleaseError(f"Not on {branch} but on {current or 'a detached HEAD'}.")
    run("git", "fetch", "-q", remote, branch)
    head = run("git", "rev-parse", "HEAD")
    if head != run("git", "rev-parse", f"{remote}/{branch}"):
        raise ReleaseError(f"HEAD differs from {remote}/{branch}, push or pull first.")
    return head


def ci_runs(head: str, branch: str, workflows: list[str]) -> list[dict] | None:
    """The runs triggered by pushing the commit to the branch, or None when GitHub is unreachable."""
    runs = []
    for workflow in workflows or [None]:
        p = subprocess.run(["gh", "run", "list", "--commit", head, "--branch", branch, "--event", "push",
                            "--limit", "100", "--json", "databaseId,workflowName,status,conclusion,url",
                            *(["--workflow", workflow] if workflow else [])], capture_output=True, text=True)
        if p.returncode:
            return None
        runs.extend(json.loads(p.stdout))
    return runs


def wait_ci(head: str, branch: str, workflows: list[str]) -> None:
    """Succeed only once all CI runs of the commit passed, waiting for those in progress."""
    short = head[:7]
    start = time.monotonic()
    announced = set()
    while True:
        runs = ci_runs(head, branch, workflows)
        if runs is None:
            say("cannot reach GitHub, trying again")
        elif not runs:
            if time.monotonic() - start > APPEAR:
                raise ReleaseError(f"No CI run for {short}, nothing released.")
        else:
            if failed := [r for r in runs if r["status"] == "completed" and r["conclusion"] != "success"]:
                raise ReleaseError("CI failed, nothing released: "
                                   + ", ".join(f"{r['workflowName']} ({r['conclusion']}) {r['url']}" for r in failed))
            pending = [r for r in runs if r["status"] != "completed"]
            for r in pending:
                if r["databaseId"] not in announced:
                    announced.add(r["databaseId"])
                    say(f"waiting for {r['workflowName']} on {short} {r['url']}")
            if not pending:
                say(f"CI passed on {short}")
                return
        time.sleep(POLL)


def main(argv=None) -> int:
    parser = ArgumentParser(prog="gh-safe-release",
                            description="Release only what passed the GitHub CI: check the remote branch and its CI,"
                            " then bump (or just tag) with bump-my-version and push.")
    parser.add_argument("part", nargs="?",
                        help="Version part to bump (patch, minor, major…): bump, commit, tag and push the commit"
                        " and the tag. Without it, the version already committed is tagged and the tag pushed.")
    parser.add_argument("-w", "--workflow", action="append", default=[],
                        help="Wait for this workflow (name or file name) only, may be repeated. Default: all of them.")
    parser.add_argument("--no-wait-ci", action="store_true", help="Do not wait for the CI (ex: a repo without one).")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="main")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Check everything, change nothing.")
    args = parser.parse_args(argv)

    try:
        config = bmv_config()
        tag = render(config["tag_name"], config["current_version"])
        if not args.part:
            check_files(config)
            if run("git", "tag", "--list", tag):
                raise ReleaseError(f"Tag {tag} exists already. Bump the version first.")
        head = preflight(args.remote, args.branch)
        if not args.no_wait_ci:
            wait_ci(head, args.branch, args.workflow)

        if args.part:
            if args.dry_run:
                print(bmv("show-bump", config["current_version"]))
                say(f"dry run, would bump {args.part}, commit, tag and push")
                return 0
            subprocess.run([sys.executable, "-m", "bumpversion", "bump", args.part, "--commit", "--tag"], check=True)
            config = bmv_config()
            tag = render(config["tag_name"], config["current_version"])
            refs = [args.branch, tag]
        else:
            if args.dry_run:
                say(f"dry run, would tag {tag} and push it")
                return 0
            run("git", "tag", "-a", tag, "-m", render(config["tag_message"], config["current_version"]))
            refs = [tag]
        subprocess.run(["git", "push", "--atomic", args.remote, *refs], check=True)
        say(f"released {tag}")
    except ReleaseError as e:
        say(str(e))
        return 1
    except subprocess.CalledProcessError as e:
        say(f"{' '.join(map(str, e.cmd))} failed")
        return 1
    return 0
