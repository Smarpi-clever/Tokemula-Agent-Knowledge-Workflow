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
            if evidence.get("kind") != "file":
                continue
            try:
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
    result = []
    for fact in engine._all("fact"):
        result.append({**fact, **status(engine, fact)})
        if include_history:
            for previous in fact.get("history", []):
                historical = {"topic": fact["topic"], "claim_id": fact["claim_id"],
                              "title": fact.get("title", fact["claim_id"]), **previous,
                              "lifecycle": "superseded", "superseded_by": previous.get("superseded_by", fact.get("version_id", "unknown"))}
                result.append({**historical, **status(engine, historical)})
    return result
