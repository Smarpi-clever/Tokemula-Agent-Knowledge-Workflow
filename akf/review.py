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
                      "linked_fact": entry.get("linked_fact"), "reason": entry.get("reason"),
                      "evidence_gap": entry.get("error", job.get("error")), "conflict_reason": conflict,
                      "proposed_change": proposed,
                      "verification_required": {"file_fact": "matching file or fixed commit hash and excerpt",
                                                "change": "fixed commit evidence",
                                                "test_result": "captured test exit receipt from this task",
                                                "execution_result": "captured execution exit receipt from this task",
                                                "deployment": "supported independent verifier"}.get(update.get("claim_type"), "objective evidence"),
                      "actions": ["show", "revalidate", "split", "reanchor", "link-history", "reject", "withdraw", "confirm"] if resolution == "pending" else ["show"],
                      "review_command": engine._argv("review", "--job", job_id, "--item", identity),
                      "read_location": {"topic": update["topic"], "claim_id": update["claim_id"]}})
    if selected is not None and not items:
        raise WorkflowError("review", "Unknown pending item")
    return {**engine._result(job), "items": items, "review_history": job.get("review_history", [])}


def review(engine, job_id: str, action: str = "show", selected: str | None = None, update: dict | None = None,
           *, reason: str | None = None, linked_fact: str | None = None, linked_version: str | None = None,
           expected_sha256: str | None = None) -> dict:
    if action == "show":
        return explain(engine, job_id, selected)
    if action not in {"revalidate", "split", "reanchor", "link-history", "reject", "withdraw", "confirm"}:
        raise WorkflowError("review", "Unknown review action")
    if reason is not None:
        from .engine import safe_text
        safe_text(reason)
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
    if action == "split":
        if not reason or not isinstance(update, dict) or update.get("status") != "partial":
            raise WorkflowError("review", "Split needs a reason and a partial candidate with two updates")
        updates = update.get("updates")
        if not isinstance(updates, list) or len(updates) != 2 or any(not isinstance(item, dict) for item in updates):
            raise WorkflowError("review", "Split needs one supported subset and one retained pending update")
        original = entry["update"]
        residual = next((item for item in updates if item.get("claim_id") == original["claim_id"]), None)
        child = next((item for item in updates if item.get("claim_id") != original["claim_id"]), None)
        if residual is None or child is None or any(item.get("topic") != original["topic"] or
                item.get("expected_sha256") != original.get("expected_sha256") for item in updates):
            raise WorkflowError("review", "Split retains the original topic and note digest")
        if not isinstance(child.get("claim_id"), str):
            raise WorkflowError("review", "Split subset needs a claim identity")
        if not str(residual.get("content", "")).startswith("Unverified:") or residual.get("evidence") != []:
            raise WorkflowError("review", "Retain unsupported scope as an explicitly unverified pending update")
        for previous in job.get("review_history", []):
            if (previous["action"] == "split" and previous.get("item_id") == selected
                    and previous.get("result") == "preparing" and previous.get("split_candidate") == update):
                result = engine.resume(job_id, only_items={selected, item_id(child)})
                job = engine._get("job", job_id)
                intent = next(item for item in job["review_history"] if item.get("at") == previous["at"])
                intent.update(child_item=item_id(child), split_candidate=copy.deepcopy(update), result=result["state"])
                engine._put("job", job_id, job)
                return explain(engine, job_id)
            if previous["action"] == "split" and previous.get("child_item") == item_id(child):
                if previous.get("split_candidate") != update:
                    raise WorkflowError("conflict", "A previously split subset cannot be silently changed")
                return explain(engine, job_id)
        if engine._get("fact", item_id(child)) or any(item_id(item["update"]) == item_id(child) for item in pending):
            raise WorkflowError("review", "Choose a new claim identity for the supported subset")
        job.pop("correction_attempts", None)
        job.setdefault("review_history", []).append({"item_id": selected, "action": action, "at": when,
            "reason": reason, "candidate_before": original, "result": "preparing"})
        engine._put("job", job_id, job)
        result = engine.submit(job_id, update, split_from=selected)
        job = engine._get("job", job_id)
        intent = next(item for item in job["review_history"] if item.get("at") == when)
        intent["result"] = result["state"]
        if result["state"] == "rejected":
            engine._put("job", job_id, job)
            return explain(engine, job_id)
        intent.update(child_item=item_id(child), split_candidate=copy.deepcopy(update), verification="tool_validation")
        engine._put("job", job_id, job)
        return explain(engine, job_id)
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
                                                     "result": result["state"], "verification": "tool_validation",
                                                     "candidate_before": entry["update"],
                                                     **({"reason": reason} if reason else {})})
        engine._put("job", job_id, job)
        return explain(engine, job_id)
    with engine._writer():
        if action == "reanchor":
            from .engine import digest
            if not reason:
                raise WorkflowError("review", "Record the reason for reviewing the current note anchor")
            candidate = entry["update"]
            path = engine._path(engine.knowledge_root, candidate["topic"], note=True)
            data = path.read_bytes() if path.exists() else None
            current = digest(data) if data is not None else None
            if expected_sha256 != current:
                raise WorkflowError("conflict", "Reviewed note digest differs; read the current note again")
            text = data.decode("utf-8") if data is not None else ""
            fact = engine._get("fact", selected)
            if fact and text.count(fact["block"]) != 1:
                raise WorkflowError("conflict", "Owned block was edited by a person")
            if not fact and "<!-- akf:" + candidate["claim_id"] + " -->" in text:
                raise WorkflowError("conflict", "Unknown managed block requires manual review")
            old_anchor = entry.get("anchor_sha256", candidate.get("expected_sha256"))
            entry["anchor_sha256"] = current
            entry.pop("anchor_write_id", None)
            job["pending_updates"] = pending
            job.setdefault("review_history", []).append({"item_id": selected, "action": action, "at": when,
                "reason": reason, "old_anchor_sha256": old_anchor, "anchor_sha256": current,
                "verification": "explicit_note_review"})
            engine._put("job", job_id, job)
            return explain(engine, job_id)
        if action == "link-history":
            from .facts import environment
            fact = engine._get("fact", linked_fact or "")
            if fact and linked_version and fact.get("version_id") != linked_version:
                fact = next((item for item in fact.get("history", []) if item.get("version_id") == linked_version), None)
            if not fact or fact.get("evidence_status") != "tool_verified":
                raise WorkflowError("review", "Select a tool-verified saved fact version")
            candidate = entry["update"]
            if (environment(fact) != engine.config.get("environment", "local")
                    or fact.get("project_root", str(engine.project_root)) != str(engine.project_root)
                    or fact.get("claim_type") != candidate.get("claim_type")):
                raise WorkflowError("review", "Historical fact scope or claim type differs")
            if candidate["content"].removeprefix("Unverified:").strip() != fact["content"].strip():
                raise WorkflowError("review", "Historical fact content differs; revalidate the selected candidate")
            if not reason:
                raise WorkflowError("review", "Record the reason for linking historical evidence")
            entry.update(withdrawn=True, resolution="linked_history", resolved_at=when,
                         linked_fact={"item_id": linked_fact, "version_id": fact["version_id"],
                                      "observed_at": fact["observed_at"]})
        elif action == "confirm":
            entry["manual_confirmation"] = {"at": when, "source": "manual_review"}
        else:
            entry.update(withdrawn=True, resolution="rejected" if action == "reject" else "withdrawn", resolved_at=when)
        if reason:
            entry["reason"] = reason
        job["pending_updates"] = pending
        job.setdefault("review_history", []).append({"item_id": selected, "action": action, "at": when,
                                                     "verification": "saved_verified_history" if action == "link-history" else "manual_confirmation" if action == "confirm" else "none",
                                                     **({"reason": reason} if reason else {}),
                                                     **({"linked_fact": entry["linked_fact"]} if action == "link-history" else {})})
        if all(entry.get("withdrawn") for entry in pending):
            job["review_closed"] = True
            job.pop("error", None)
            if not job.get("plans"):
                job["state"] = "rejected" if action == "reject" else "cancelled"
            elif engine._write_complete(job):
                job["state"] = job.get("write_state", job["state"])
        engine._put("job", job_id, job)
    return explain(engine, job_id)
