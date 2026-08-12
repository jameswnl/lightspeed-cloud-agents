#!/usr/bin/env python3
"""Poll GitHub repos for changed PRs and plan-created issues.

This script is meant to be used by an external watcher loop. It avoids the
overly narrow "needs review by this bot" semantics and instead detects:

- new open pull requests
- new commits on existing open pull requests
- metadata-only updates on open pull requests
- new open issues with a target label (default: plan-created)
- updates to those labeled issues

The script persists a small JSON state file between runs.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_STATE_FILE = Path(
    os.environ.get(
        "PR_WATCH_STATE_FILE",
        Path.home() / ".cursor" / "repo-watch-state.json",
    )
)


@dataclass
class PullRequestSnapshot:
    number: int
    title: str
    url: str
    author: str
    updated_at: str
    head_sha: str
    base_ref: str
    head_ref: str
    draft: bool


@dataclass
class IssueSnapshot:
    number: int
    title: str
    url: str
    updated_at: str
    labels: list[str]


class WatchFetchError(RuntimeError):
    """Raised when GitHub data cannot be fetched for a repo."""


def _run_json(command: list[str], cwd: Path | None = None) -> Any:
    last_error: subprocess.CalledProcessError | None = None
    for _attempt in range(2):
        try:
            proc = subprocess.run(
                command,
                cwd=str(cwd) if cwd else None,
                check=True,
                capture_output=True,
                text=True,
            )
            return json.loads(proc.stdout)
        except subprocess.CalledProcessError as exc:
            last_error = exc
    stderr = (last_error.stderr or "").strip() if last_error else ""
    raise WatchFetchError(
        f"Command failed: {' '.join(command)}"
        + (f" | stderr: {stderr}" if stderr else "")
    ) from last_error


def _is_issues_disabled_error(exc: BaseException) -> bool:
    message = str(exc)
    return (
        "has disabled issues" in message
        or "Issues are disabled for this repo" in message
        or "HTTP 410" in message
        or "HTTP 404" in message
        or "HTTP 400" in message
    )


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"repos": {}}
    return json.loads(path.read_text())


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def _fetch_open_prs(repo: str) -> list[PullRequestSnapshot]:
    payload = _run_json(
        [
            "gh",
            "api",
            f"repos/{repo}/pulls?state=open&per_page=100",
        ]
    )
    snapshots: list[PullRequestSnapshot] = []
    for pr in payload:
        snapshots.append(
            PullRequestSnapshot(
                number=pr["number"],
                title=pr["title"],
                url=pr["html_url"],
                author=pr["user"]["login"],
                updated_at=pr["updated_at"],
                head_sha=pr["head"]["sha"],
                base_ref=pr["base"]["ref"],
                head_ref=pr["head"]["ref"],
                draft=bool(pr.get("draft", False)),
            )
        )
    return snapshots


def _fetch_labeled_issues(repo: str, label: str) -> list[IssueSnapshot]:
    payload = _run_json(
        [
            "gh",
            "api",
            f"repos/{repo}/issues?state=open&labels={label}&per_page=100",
        ]
    )
    snapshots: list[IssueSnapshot] = []
    for issue in payload:
        if "pull_request" in issue:
            continue
        snapshots.append(
            IssueSnapshot(
                number=issue["number"],
                title=issue["title"],
                url=issue["html_url"],
                updated_at=issue["updated_at"],
                labels=[lbl["name"] for lbl in issue.get("labels", [])],
            )
        )
    return snapshots


def _diff_prs(
    previous: dict[str, Any],
    current: list[PullRequestSnapshot],
) -> dict[str, list[dict[str, Any]]]:
    new_prs: list[dict[str, Any]] = []
    new_commits: list[dict[str, Any]] = []
    metadata_updates: list[dict[str, Any]] = []

    for pr in current:
        old = previous.get(str(pr.number))
        item = asdict(pr)
        if old is None:
            new_prs.append(item)
            continue
        if old.get("head_sha") != pr.head_sha:
            new_commits.append(
                {
                    **item,
                    "previous_head_sha": old.get("head_sha"),
                }
            )
        elif old.get("updated_at") != pr.updated_at:
            metadata_updates.append(item)

    return {
        "new_prs": new_prs,
        "new_commits": new_commits,
        "metadata_updates": metadata_updates,
    }


def _diff_issues(
    previous: dict[str, Any],
    current: list[IssueSnapshot],
) -> dict[str, list[dict[str, Any]]]:
    new_issues: list[dict[str, Any]] = []
    updated_issues: list[dict[str, Any]] = []

    for issue in current:
        old = previous.get(str(issue.number))
        item = asdict(issue)
        if old is None:
            new_issues.append(item)
            continue
        if old.get("updated_at") != issue.updated_at or old.get("labels") != issue.labels:
            updated_issues.append(item)

    return {
        "new_issues": new_issues,
        "updated_issues": updated_issues,
    }


def _serialize_repo_state(
    prs: list[PullRequestSnapshot],
    issues: list[IssueSnapshot],
) -> dict[str, Any]:
    return {
        "prs": {str(pr.number): asdict(pr) for pr in prs},
        "issues": {str(issue.number): asdict(issue) for issue in issues},
    }


def build_report(
    repos: list[str],
    label: str,
    state_file: Path,
) -> dict[str, Any]:
    old_state = _load_state(state_file)
    report: dict[str, Any] = {
        "status": "quiet",
        "repos": {},
    }
    new_state: dict[str, Any] = {"repos": {}}

    for repo in repos:
        prev_repo = old_state.get("repos", {}).get(repo, {})
        try:
            prs = _fetch_open_prs(repo)
        except WatchFetchError as exc:
            report["repos"][repo] = {
                "pull_requests": {
                    "new_prs": [],
                    "new_commits": [],
                    "metadata_updates": [],
                },
                "issues": {
                    "new_issues": [],
                    "updated_issues": [],
                },
                "fetch_error": str(exc),
            }
            report["status"] = "fetch_error"
            if prev_repo:
                new_state["repos"][repo] = prev_repo
            continue

        try:
            issues = _fetch_labeled_issues(repo, label)
        except WatchFetchError as exc:
            if _is_issues_disabled_error(exc):
                issues = []
            else:
                report["repos"][repo] = {
                    "pull_requests": {
                        "new_prs": [],
                        "new_commits": [],
                        "metadata_updates": [],
                    },
                    "issues": {
                        "new_issues": [],
                        "updated_issues": [],
                    },
                    "fetch_error": str(exc),
                }
                report["status"] = "fetch_error"
                if prev_repo:
                    new_state["repos"][repo] = {
                        "prs": {str(pr.number): asdict(pr) for pr in prs},
                        "issues": prev_repo.get("issues", {}),
                    }
                continue

        pr_changes = _diff_prs(prev_repo.get("prs", {}), prs)
        issue_changes = _diff_issues(prev_repo.get("issues", {}), issues)

        repo_report = {
            "pull_requests": pr_changes,
            "issues": issue_changes,
        }
        report["repos"][repo] = repo_report

        if any(repo_report["pull_requests"].values()) or any(repo_report["issues"].values()):
            report["status"] = "changes_detected"

        new_state["repos"][repo] = _serialize_repo_state(prs, issues)

    _save_state(state_file, new_state)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        action="append",
        required=True,
        help="GitHub repo in owner/name form. Repeat for multiple repos.",
    )
    parser.add_argument(
        "--issue-label",
        default="plan-created",
        help="Issue label to watch for issue updates. Default: plan-created",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help=f"State file path. Default: {DEFAULT_STATE_FILE}",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(
        repos=args.repo,
        label=args.issue_label,
        state_file=args.state_file,
    )
    if args.pretty:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
