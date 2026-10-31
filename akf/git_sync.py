"""Exact-path Git commits and retryable, authorization-bound synchronization."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess


class SyncError(Exception):
    def __init__(self, state: str, message: str):
        self.state = state
        super().__init__(message)


def _run(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30,
                                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    except subprocess.TimeoutExpired:
        raise SyncError("retry", "Git operation timed out; local state retained") from None
    if check and result.returncode:
        raise SyncError("retry", "Git command failed; local state retained")
    return result


def _verify_binding(root: Path, settings: dict) -> tuple[str, str, str]:
    remote, branch, expected = (settings.get(k) for k in ("remote", "branch", "remote_url"))
    if not all(isinstance(v, str) and v for v in (remote, branch, expected)):
        raise SyncError("conflict", "Git synchronization requires exact remote, URL and branch bindings")
    if remote.startswith("-") or branch.startswith("-"):
        raise SyncError("conflict", "Invalid Git binding")
    actual_root = Path(_run(root, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if actual_root != root.resolve():
        raise SyncError("conflict", "Knowledge path is not the authorized repository root")
    actual_branch = _run(root, "symbolic-ref", "--short", "HEAD").stdout.strip()
    urls = _run(root, "remote", "get-url", "--all", remote).stdout.splitlines()
    push_urls = _run(root, "remote", "get-url", "--push", "--all", remote).stdout.splitlines()
    if actual_branch != branch or urls != [expected] or push_urls != [expected]:
        raise SyncError("conflict", "Git path, branch or remote changed")
    if settings.get("auto_push", False):
        repository = settings.get("repository")
        if settings.get("transport") == "local":
            if not Path(expected).is_absolute() or not Path(expected).is_dir():
                raise SyncError("conflict", "Local fixture remote must be an existing absolute directory")
        else:
            if not isinstance(repository, str) or repository.count("/") != 1 or repository.startswith("-"):
                raise SyncError("conflict", "Private GitHub repository identity is required")
            valid_urls = {"git@github.com:" + repository + ".git", "https://github.com/" + repository + ".git"}
            if expected not in valid_urls:
                raise SyncError("conflict", "Remote URL differs from repository authorization")
            result = subprocess.run(["gh", "api", "repos/" + repository], capture_output=True, text=True)
            try:
                meta = json.loads(result.stdout) if result.returncode == 0 else {}
            except ValueError:
                meta = {}
            if meta.get("full_name") != repository or meta.get("private") is not True:
                raise SyncError("retry", "Private repository identity could not be confirmed")
    return remote, branch, expected


def _head(root: Path) -> str:
    result = _run(root, "rev-parse", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def _remote_head(root: Path, remote: str, branch: str) -> str:
    output = _run(root, "ls-remote", "--exit-code", remote, "refs/heads/" + branch, check=False)
    if output.returncode == 2:
        return ""
    if output.returncode:
        raise SyncError("retry", "Remote confirmation failed; retry will query it before pushing")
    lines = output.stdout.splitlines()
    return lines[0].split()[0] if len(lines) == 1 else ""


def _verify_files(root: Path, plans: list, commit: str | None = None) -> None:
    for plan in plans:
        if commit:
            result = subprocess.run(["git", "-C", str(root), "show", commit + ":" + plan["topic"]], capture_output=True)
            if result.returncode:
                raise SyncError("conflict", "Owned note is missing from the commit")
            data = result.stdout
            entry = _run(root, "ls-tree", "-z", commit, "--", plan["topic"]).stdout.split("\0")
            entries = [item.split("\t", 1)[0].split() for item in entry if item]
            expected_mode = "100755" if plan["mode"] & 0o100 else "100644"
            if len(entries) != 1 or entries[0][:2] != [expected_mode, "blob"]:
                raise SyncError("conflict", "Owned note type or executable mode differs from the plan")
        else:
            data = (root / plan["topic"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != plan["after_sha256"]:
            raise SyncError("conflict", "Owned note content changed; refusing to stage or publish")


def synchronize(root: Path, settings: dict, job: dict, save) -> dict:
    remote, branch, _ = _verify_binding(root, settings)
    paths = sorted(plan["topic"] for plan in job["plans"])
    _verify_files(root, job["plans"])
    marker = "AKF-Job: " + job.get("write_id", job["id"])
    if not job.get("commit"):
        # A process may stop after git commit but before saving its result.
        current = _head(root)
        if current and marker in _run(root, "show", "-s", "--format=%B", current).stdout.splitlines():
            _verify_files(root, job["plans"], current)
            job["commit"] = current
        else:
            if _run(root, "diff", "--cached", "--name-only").stdout.strip():
                staged = _run(root, "diff", "--cached", "--name-only", "-z").stdout.split("\0")
                if not job.get("stage_owned") or sorted(p for p in staged if p) != paths:
                    raise SyncError("conflict", "Existing staged work is not owned by this job")
                for plan in job["plans"]:
                    staged_data = subprocess.run(["git", "-C", str(root), "show", ":" + plan["topic"]], capture_output=True).stdout
                    if hashlib.sha256(staged_data).hexdigest() != plan["after_sha256"]:
                        raise SyncError("conflict", "Owned staging was changed by a person")
            if not job.get("commit_parent"):
                job["commit_parent"] = current
                save(job)
            elif current != job["commit_parent"]:
                raise SyncError("conflict", "Repository advanced during pending commit")
            for plan in job["plans"]:
                if current:
                    original = subprocess.run(["git", "-C", str(root), "show", current + ":" + plan["topic"]], capture_output=True)
                    original_hash = hashlib.sha256(original.stdout).hexdigest() if original.returncode == 0 else None
                    if original_hash != plan["before_sha256"]:
                        raise SyncError("conflict", "Note contained pre-existing human changes; keep local save uncommitted")
            job["stage_owned"] = True
            save(job)
            _run(root, "add", "--", *paths)
            staged = sorted(p for p in _run(root, "diff", "--cached", "--name-only", "-z").stdout.split("\0") if p)
            if staged != paths:
                raise SyncError("conflict", "Staged paths differ from the exact owned update")
            for plan in job["plans"]:
                data = subprocess.run(["git", "-C", str(root), "show", ":" + plan["topic"]], capture_output=True).stdout
                if hashlib.sha256(data).hexdigest() != plan["after_sha256"]:
                    raise SyncError("conflict", "Staged bytes differ from the verified update")
            _run(root, "commit", "-m", "docs: update verified knowledge\n\n" + marker)
            job["commit"] = _head(root)
    # Every path, including recovery with a saved commit, must pass the same publication gate.
    _verify_files(root, job["plans"], job["commit"])
    actual = sorted(p for p in _run(root, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", job["commit"]).stdout.split("\0") if p)
    parents = _run(root, "show", "-s", "--format=%P", job["commit"]).stdout.strip().split()
    expected_parents = [job["commit_parent"]] if job.get("commit_parent") else []
    if actual != paths or parents != expected_parents:
        raise SyncError("conflict", "Commit does not match the owned change and parent")
    job["state"] = job["write_state"] = "committed"
    save(job)
    if not settings.get("auto_push", False):
        return {"state": "committed", "commit": job["commit"]}
    if _head(root) != job["commit"]:
        raise SyncError("conflict", "Local branch advanced; review before publishing")
    remote_sha = _remote_head(root, remote, branch)
    if remote_sha != job["commit"]:
        if remote_sha and remote_sha != job.get("commit_parent", ""):
            raise SyncError("conflict", "Remote advanced; no automatic merge or force push")
        job["push_attempted"] = True
        save(job)
        result = _run(root, "push", remote, "HEAD:refs/heads/" + branch, check=False)
        # Even a failing transport can have delivered the update.
        confirmed = _remote_head(root, remote, branch)
        if confirmed != job["commit"]:
            raise SyncError("retry" if result.returncode else "conflict", "Push not confirmed; local commit retained")
    _run(root, "fetch", "--no-tags", remote, "refs/heads/" + branch)
    fetched = _run(root, "rev-parse", "FETCH_HEAD").stdout.strip()
    if fetched != job["commit"]:
        raise SyncError("conflict", "Remote changed during readback")
    _verify_files(root, job["plans"], fetched)
    return {"state": "remote_confirmed", "commit": job["commit"], "remote_head": fetched}
