"""Item-level review retains evidence, owned anchors, and successful sibling writes."""
from __future__ import annotations

import copy
import datetime as dt

from .errors import WorkflowError


def item_id(update: dict) -> str:
    return update["topic"] + ":" + update["claim_id"]


def entries(job: dict) -> list[dict]:
    result = copy.deepcopy(job.get("pending_updates", []))
    known = {item_id(entry["update"]) for entry in result}
    resolved = job.get("resolved_updates", {})
    if not job.get("review_closed") and job.get("state") in {"candidate_pending", "pending_evidence", "conflict", "stale", "busy"}:
        for update in job.get("candidate", {}).get("updates", []):
            identity = item_id(update)
            if identity not in known and identity not in resolved:
                result.append({"update": copy.deepcopy(update), "error": job.get("error", "Candidate awaits validation")})
                known.add(identity)
    return result


def explain(engine, job_id: str, selected: str | None = None) -> dict:
    job = engine._get("job", job_id)
    if not job:
        raise WorkflowError("job", "Unknown maintenance job")
    items = []
    for entry in entries(job):
        update = entry["update"]
        identity = item_id(update)
        if selected is not None and identity != selected:
            continue
        previous = engine._get("fact", identity)
        resolution = entry.get("resolution", "withdrawn" if entry.get("withdrawn") else "pending")
        conflict = job.get("error") if job["state"] in {"conflict", "stale"} else None
        proposed = "add" if previous is None else "reverify" if previous["content"].strip() == update["content"].strip() else "replace"
        items.append({"item_id": identity, "resolution": resolution, "candidate": update,
                      "target_fact": {**previous, **engine.fact_status(previous)} if previous else None,
                      "sources": update.get("evidence", []), "evidence_status": "unverified",
                      "manual_confirmation": entry.get("manual_confirmation"),
                      "evidence_gap": entry.get("error", job.get("error")), "conflict_reason": conflict,
                      "proposed_change": proposed,
                      "verification_required": {"file_fact": "matching file or fixed commit hash and excerpt",
                                                "change": "fixed commit evidence",
                                                "test_result": "captured test exit receipt from this task",
                                                "execution_result": "captured execution exit receipt from this task",
                                                "deployment": "supported independent verifier"}.get(update.get("claim_type"), "objective evidence"),
                      "actions": ["show", "revalidate", "reject", "withdraw", "confirm"] if resolution == "pending" else ["show"],
                      "review_command": engine._argv("review", "--job", job_id, "--item", identity),
                      "read_location": {"topic": update["topic"], "claim_id": update["claim_id"]}})
    if selected is not None and not items:
        raise WorkflowError("review", "Unknown pending item")
    return {**engine._result(job), "items": items, "review_history": job.get("review_history", [])}


def review(engine, job_id: str, action: str = "show", selected: str | None = None, update: dict | None = None) -> dict:
    if action == "show":
        return explain(engine, job_id, selected)
    if action not in {"revalidate", "reject", "withdraw", "confirm"}:
        raise WorkflowError("review", "Unknown review action")
    if selected is None:
        raise WorkflowError("review", "Select one item with --item")
    job = engine._get("job", job_id)
    if not job:
        raise WorkflowError("job", "Unknown maintenance job")
    if not engine._active():
        return {**engine._result(job), "state": "paused"}
    pending = entries(job)
    entry = next((entry for entry in pending if item_id(entry["update"]) == selected), None)
    if entry is None:
        raise WorkflowError("review", "Unknown pending item")
    if entry.get("withdrawn"):
        return explain(engine, job_id, selected)
    when = dt.datetime.now(dt.timezone.utc).isoformat()
    if action == "revalidate":
        candidate = copy.deepcopy(entry["update"] if update is None else update)
        if not isinstance(candidate, dict) or candidate.get("topic") != entry["update"]["topic"] or candidate.get("claim_id") != entry["update"]["claim_id"]:
            raise WorkflowError("review", "Revalidation must retain the selected topic and claim identity")
        if candidate.get("expected_sha256") != entry["update"].get("expected_sha256"):
            raise WorkflowError("conflict", "Review retains the original note digest; read and submit a new task after a human edit")
        job["pending_updates"] = pending
        job.pop("correction_attempts", None)
        engine._put("job", job_id, job)
        result = engine.submit(job_id, {"status": job.get("candidate", {}).get("status", "partial"), "updates": [candidate]}, only_item=selected)
        job = engine._get("job", job_id)
        job.setdefault("review_history", []).append({"item_id": selected, "action": action, "at": when,
                                                     "result": result["state"], "verification": "tool_validation"})
        engine._put("job", job_id, job)
        return explain(engine, job_id)
    with engine._writer():
        if action == "confirm":
            entry["manual_confirmation"] = {"at": when, "source": "manual_review"}
        else:
            entry.update(withdrawn=True, resolution="rejected" if action == "reject" else "withdrawn", resolved_at=when)
        job["pending_updates"] = pending
        job.setdefault("review_history", []).append({"item_id": selected, "action": action, "at": when,
                                                     "verification": "manual_confirmation" if action == "confirm" else "none"})
        if all(entry.get("withdrawn") for entry in pending):
            job["review_closed"] = True
            job.pop("error", None)
            if not job.get("plans"):
                job["state"] = "rejected" if action == "reject" else "cancelled"
            elif engine._write_complete(job):
                job["state"] = job.get("write_state", job["state"])
        engine._put("job", job_id, job)
    return explain(engine, job_id)
