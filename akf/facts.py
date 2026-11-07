"""Fact versions and freshness are independent of maintenance task outcomes."""
from __future__ import annotations

import datetime as dt
import hashlib
import re

from .errors import WorkflowError


def environment(fact: dict) -> str:
    value = fact.get("environment")
    if value:
        return value
    match = re.search(r"^Environment: (.+)$", fact.get("block", ""), re.MULTILINE)
    return match.group(1) if match else "unknown"


def status(engine, fact: dict) -> dict:
    reasons = []
    lifecycle = fact.get("lifecycle", "current")
    evidence_status = fact.get("evidence_status", "unknown")
    unknown_rule = False
    if lifecycle != "superseded":
        for evidence in fact.get("evidence", []):
            if evidence.get("kind") not in {"file", "report"}:
                continue
            try:
                if evidence.get("kind") == "report":
                    from .reports import report_path
                    path = report_path(engine, evidence["path"])
                else:
                    path = engine._path(engine.project_root, evidence["path"])
                if not path.is_file():
                    reasons.append("source_missing:" + evidence["path"])
                elif path.stat().st_size > 2_000_000 or hashlib.sha256(path.read_bytes()).hexdigest() != evidence.get("sha256"):
                    reasons.append("file_changed:" + evidence["path"])
            except (OSError, ValueError, WorkflowError):
                reasons.append("source_unavailable:" + evidence.get("path", "unknown"))
        review_after = fact.get("review_after")
        if review_after:
            try:
                deadline = dt.datetime.fromisoformat(review_after.replace("Z", "+00:00"))
                if deadline.tzinfo is None:
                    raise ValueError("Review deadline has no timezone")
                if deadline <= dt.datetime.now(dt.timezone.utc):
                    reasons.append("review_after")
            except (AttributeError, TypeError, ValueError):
                unknown_rule = True
                reasons.append("review_after_unknown")
    return {"evidence_status": evidence_status, "lifecycle": lifecycle,
            "review_status": "unknown" if unknown_rule else "review_required" if reasons else "clear" if evidence_status == "tool_verified" else "unknown",
            "review_reasons": reasons, "last_verified_at": fact.get("last_verified_at"),
            "environment": environment(fact), "version_id": fact.get("version_id", "unknown"),
            "superseded_by": fact.get("superseded_by")}


def records(engine, include_history: bool = False) -> list[dict]:
    current = [{**fact, **status(engine, fact)} for fact in engine._all("fact")]
    by_id = {fact["topic"] + ":" + fact["claim_id"]: fact for fact in current}
    from .engine import timestamp
    relationship_gaps = []
    for fact in current:
        if fact.get("evidence_status") != "tool_verified":
            continue
        for target in fact.get("supersedes", []):
            previous = by_id.get(target)
            bound = fact.get("supersedes_versions", {}).get(target)
            matches = bool(previous and
                           previous.get("project_root", str(engine.project_root)) == fact.get("project_root", str(engine.project_root)) and
                           environment(previous) in {"unknown", environment(fact)} and
                           all(previous.get(key) in {None, fact.get(key)} for key in ("matter_id", "state_kind")) and
                           timestamp(previous["observed_at"]) <= timestamp(fact["observed_at"]) and
                           (bound is None or bound == {"version_id": previous.get("version_id", "unknown"),
                                                       "observed_at": previous["observed_at"]}))
            if not matches:
                relationship_gaps.append((fact, target))
                continue
            previous.update(lifecycle="superseded", superseded_by=fact["version_id"],
                            superseded_by_claim=fact["topic"] + ":" + fact["claim_id"])
            previous.update(status(engine, previous))
    for fact, target in relationship_gaps:
        if fact["lifecycle"] == "current":
            fact["review_status"] = "review_required"
            fact["review_reasons"].append("superseded_target_changed:" + target)
    groups = {}
    for fact in current:
        if fact.get("matter_id") and fact.get("state_kind") in {"progress", "commit", "release", "installation", "deployment"} and fact["lifecycle"] == "current":
            scope = (fact.get("project_root"), fact["environment"], fact["matter_id"], fact["state_kind"])
            groups.setdefault(scope, []).append(fact)
    for group in groups.values():
        if len(group) > 1 and len({fact["content"].strip() for fact in group}) > 1:
            for fact in group:
                fact["review_status"] = "review_required"
                fact["review_reasons"].append("unresolved_state_relationship")
                fact["related_current"] = [other["topic"] + ":" + other["claim_id"] for other in group if other is not fact]
    result = []
    for fact in current:
        result.append(fact)
        if include_history:
            for previous in fact.get("history", []):
                historical = {"topic": fact["topic"], "claim_id": fact["claim_id"],
                              "title": fact.get("title", fact["claim_id"]), **previous,
                              "lifecycle": "superseded", "superseded_by": previous.get("superseded_by", fact.get("version_id", "unknown"))}
                result.append({**historical, **status(engine, historical)})
    return result


SCOPE_FIELDS = ("matter_id", "state_kind", "stage", "supersedes")
STATE_KINDS = {"behavior", "progress", "source", "test", "commit", "release", "installation", "deployment"}


def validate_scope(update: dict) -> None:
    """Relationship metadata is optional for existing candidates."""
    from .engine import safe_text
    if not any(key in update for key in SCOPE_FIELDS):
        return
    matter = update.get("matter_id")
    if not isinstance(matter, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", matter):
        raise WorkflowError("candidate", "State relationships need a stable matter_id")
    if not isinstance(update.get("state_kind"), str) or update["state_kind"] not in STATE_KINDS:
        raise WorkflowError("candidate", "State relationships need an explicit state_kind")
    if "stage" in update:
        safe_text(update["stage"])
        if not update["stage"] or len(update["stage"]) > 120 or "\n" in update["stage"]:
            raise WorkflowError("candidate", "Invalid state stage")
    targets = update.get("supersedes", [])
    if not isinstance(targets, list) or len(targets) > 20:
        raise WorkflowError("candidate", "State replacement needs bounded unique item identities")
    for target in targets:
        if not isinstance(target, str) or not re.fullmatch(r"topics/[^:]+\.md:[a-z0-9][a-z0-9_.-]{0,63}", target):
            raise WorkflowError("candidate", "Invalid related fact identity")
        if target == update["topic"] + ":" + update["claim_id"]:
            raise WorkflowError("candidate", "A fact cannot replace itself across claim identities")
    if len(set(targets)) != len(targets):
        raise WorkflowError("candidate", "State replacement needs unique item identities")


def validate_relationships(engine, update: dict) -> dict:
    from .engine import timestamp
    expected_type = {"commit": "change", "test": "test_result", "deployment": "deployment"}.get(update.get("state_kind"))
    if expected_type and update["claim_type"] != expected_type:
        raise WorkflowError("pending_evidence", update["state_kind"] + " state requires " + expected_type + " evidence")
    versions = {}
    for target in update.get("supersedes", []):
        previous = engine._get("fact", target)
        if not previous:
            raise WorkflowError("pending_evidence", "Related fact is unavailable: " + target)
        if (previous.get("project_root", str(engine.project_root)) != str(engine.project_root)
                or environment(previous) not in {"unknown", engine.config.get("environment", "local")}):
            raise WorkflowError("conflict", "Related fact belongs to another project or environment")
        if any(previous.get(key) not in {None, update.get(key)} for key in ("matter_id", "state_kind")):
            raise WorkflowError("conflict", "Related facts need the same matter and state kind")
        if timestamp(update["observed_at"]) < timestamp(previous["observed_at"]):
            raise WorkflowError("stale", "Older state evidence cannot replace a newer related fact")
        versions[target] = {"version_id": previous.get("version_id", "unknown"), "observed_at": previous["observed_at"]}
        # Reusing the same observation must not create a cycle; a reopened version is independent.
        remaining = list(previous.get("supersedes", []))
        visited = set()
        identity = update["topic"] + ":" + update["claim_id"]
        while remaining:
            key = remaining.pop()
            if key == identity:
                existing = engine._get("fact", identity)
                if existing and update["content"].strip() == existing["content"].strip() and update["observed_at"] == existing["observed_at"]:
                    raise WorkflowError("conflict", "State replacement would create a relationship cycle")
                continue
            if key not in visited:
                visited.add(key)
                older = engine._get("fact", key)
                if older:
                    remaining.extend(older.get("supersedes", []))
    return versions
