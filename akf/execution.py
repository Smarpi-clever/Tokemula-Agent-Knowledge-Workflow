"""Attribute test commands and supplement selected receipts via read-only native APIs."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
from contextlib import ExitStack
from pathlib import Path
import re
import shlex

from .errors import WorkflowError
from .install import InstallError
from .native import NativeClient


def _shell_body(command: str) -> str:
    for _ in range(3):
        try:
            words = shlex.split(command)
        except ValueError:
            break
        if (len(words) == 3 and Path(words[0]).name in {"sh", "bash", "zsh", "dash", "ksh"}
                and words[1] in {"-c", "-lc", "-ic"}):
            command = words[2]
        else:
            break
    return command


def _test_parts(command: str):
    if not isinstance(command, str):
        return None
    command = _shell_body(command)
    if any(token in command for token in ("\n", "`", "$(")):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        words = list(lexer)
    except ValueError:
        return None
    if any(word and set(word) <= set("();<>|&") for word in words):
        return None
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=[^\n]*", words[0]):
        words.pop(0)
    if words and words[0] == "exec":
        words.pop(0)
    if words and Path(words[0]).name == "env":
        words.pop(0)
        while words:
            if words[0] == "-u" and len(words) > 1:
                del words[:2]
            elif words[0] == "--" or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=[^\n]*", words[0]):
                words.pop(0)
            else:
                break
    if not words:
        return None
    executable, args = Path(words[0]).name, words[1:]
    if any(arg in {"-h", "--help", "--version", "-V"} for arg in args):
        return None
    if executable in {"pytest", "pytest-3"}:
        return "pytest", args
    if executable == "go" and args[:1] == ["test"]:
        return "go", args[1:]
    if executable in {"npm", "pnpm"} and (args[:1] == ["test"] or args[:2] == ["run", "test"]):
        return executable + " test", args[1:] if args[0] == "test" else args[2:]
    if executable == "pnpm" and args[:1] == ["exec"]:
        args = args[1:]
        executable, args = (args[0], args[1:]) if args else ("", [])
    elif executable in {"pnpm", "npx"} and args[:1] == ["vitest"]:
        executable, args = args[0], args[1:]
    if executable == "vitest" and args[:1] == ["run"] and "-v" not in args:
        return "vitest", args[1:]
    if re.fullmatch(r"python[0-9.]*", executable):
        while args and args[0] in {"-B", "-I", "-u"}:
            args.pop(0)
        if args[:2] in (["-m", "unittest"], ["-m", "pytest"]):
            return args[1], args[2:]
    return None


def test_command(command: str) -> bool:
    """Recognize one test process; compound command exits have no per-test attribution."""
    return _test_parts(command) is not None


def test_details(command: str, output: str | None = None) -> dict | None:
    """Keep command selection and only statistics reported by this exact native item."""
    parts = _test_parts(command)
    if parts is None:
        return None
    runner, arguments = parts
    result = {"runner": runner, "scope": {"arguments": arguments}, "count_state": "unknown"}
    from .engine import safe_text
    try:
        safe_text(shlex.join(arguments))
        if any(re.match(r"(?i)^--?(?:api[-_]?key|password|secret|(?:access[-_]?)?token)(?:=|$)", arg) for arg in arguments):
            raise WorkflowError("unsafe_text", "Sensitive test selection")
    except WorkflowError:
        result["scope"] = {"state": "sensitive_selection_omitted"}
    if not isinstance(output, str):
        return result
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    if runner == "unittest":
        summaries = re.findall(r"(?m)^Ran (\d+) tests? in [\d.]+s\s*$", output)
        if len(summaries) == 1:
            result.update(count_state="reported", statistics={"tests": {"total": int(summaries[0])}})
    elif runner == "vitest":
        summaries = re.findall(r"(?m)^\s*(Tests|Test Files)\s+([^\n]+?)\s*\((\d+)\)\s*$", output)
        statistics = {}
        for label, counts, total in summaries:
            key = "tests" if label == "Tests" else "test_files"
            if key in statistics:
                return result
            values = {name: int(count) for count, name in re.findall(r"(\d+)\s+(passed|failed|skipped|todo)", counts)}
            if not values or sum(values.values()) > int(total):
                return result
            statistics[key] = {"total": int(total), **values}
        if statistics:
            result.update(count_state="reported", statistics=statistics)
    return result


def recover_receipt(receipt: dict, project_root: Path, *, cache: dict | None = None, client=None) -> dict:
    """Return a supplemented copy. Cache is scoped to one maintenance/review operation."""
    if receipt.get("exit_code") is not None:
        return copy.deepcopy(receipt)
    result = copy.deepcopy(receipt)
    cache = {} if cache is None else cache
    project_root = Path(project_root).resolve()
    session, turn, tool = (receipt.get(key) for key in ("session_id", "turn_id", "tool_use_id"))
    provenance = {"source": "codex_app_server", "session_id": session, "turn_id": turn,
                  "tool_use_id": tool, "project_root": str(project_root),
                  "supplemented_at": dt.datetime.now(dt.timezone.utc).isoformat(), "query_count": 0}
    result["native_evidence"] = provenance
    result["execution_state"] = "unknown"

    def gap(reason):
        provenance["reason"] = reason
        return result

    if not all(isinstance(value, str) and value for value in (session, turn, tool)):
        return gap("missing_receipt_identity")
    if receipt.get("tool_name") != "Bash":
        return gap("unsupported_native_tool")
    if not re.fullmatch(r"[0-9a-f]{64}", receipt.get("command_sha256", "")):
        return gap("missing_command_identity")
    failure_key = ("native_error", session)
    if failure_key in cache:
        return gap(cache[failure_key])
    with ExitStack() as stack:
        def request(method, params):
            nonlocal client
            if client is None:
                client = stack.enter_context(NativeClient())
            provenance["query_count"] += 1
            return client.request(method, params)

        try:
            thread_key = ("thread", session)
            if thread_key not in cache:
                cache[thread_key] = request("thread/read", {"threadId": session, "includeTurns": False}).get("thread", {})
            thread = cache[thread_key]
            if thread.get("id") != session:
                return gap("session_identity_mismatch")
            cwd = thread.get("cwd")
            if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).resolve().is_relative_to(project_root):
                return gap("project_identity_mismatch")
            items_key = ("items", session, turn)
            if items_key not in cache:
                rows, cursor, seen = [], None, set()
                for _ in range(10):
                    params = {"threadId": session, "turnId": turn, "limit": 20, "sortDirection": "desc"}
                    if cursor:
                        params["cursor"] = cursor
                    page = request("thread/items/list", params)
                    rows.extend(page.get("data", []))
                    cursor = page.get("nextCursor")
                    if not cursor or cursor in seen:
                        break
                    seen.add(cursor)
                cache[items_key] = {"rows": rows, "limited": bool(cursor)}
            page = cache[items_key]
        except (InstallError, OSError, ValueError, KeyError, TypeError) as exc:
            cache[failure_key] = "native_query_failed: " + str(exc)
            return gap(cache[failure_key])
    matches = [row for row in page["rows"] if isinstance(row, dict)
               and isinstance(row.get("item"), dict) and row["item"].get("id") == tool]
    if not matches:
        return gap("native_item_query_limit" if page["limited"] else "native_tool_not_found")
    if len(matches) != 1:
        return gap("ambiguous_native_tool_identity")
    row = matches[0]
    if row.get("turnId") != turn:
        return gap("turn_identity_mismatch")
    item = row["item"]
    if item.get("type") != "commandExecution":
        return gap("native_tool_type_mismatch")
    command = item.get("command")
    if not isinstance(command, str) or receipt["command_sha256"] not in {
            hashlib.sha256(value.encode()).hexdigest() for value in (command, _shell_body(command))}:
        return gap("command_identity_mismatch")
    command_cwd = item.get("cwd")
    if not isinstance(command_cwd, str) or not Path(command_cwd).is_absolute() or not Path(command_cwd).resolve().is_relative_to(project_root):
        return gap("command_project_mismatch")
    if receipt.get("command_cwd") and Path(command_cwd).resolve() != Path(receipt["command_cwd"]).resolve():
        return gap("command_cwd_mismatch")
    provenance.update(command_sha256=receipt["command_sha256"], command_cwd=command_cwd,
                      executed_at=receipt.get("observed_at"), execution_time_source="hook_observed_at",
                      native_status=item.get("status"), method="thread/items/list")
    for field in ("startedAtMs", "completedAtMs"):
        value = row.get(field, item.get(field))
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 253_402_300_799_000:
            provenance[field] = value
    if provenance.get("completedAtMs") is not None:
        provenance["executed_at"] = dt.datetime.fromtimestamp(provenance["completedAtMs"] / 1000, dt.timezone.utc).isoformat()
        provenance["execution_time_source"] = "native_completedAtMs"
    details = test_details(command, item.get("aggregatedOutput"))
    result["classification"] = "test" if details is not None else "execution"
    if details is not None:
        result["test_evidence"] = {**details, "command_sha256": receipt["command_sha256"],
                                   "statistics_source": "native_command_aggregatedOutput" if details["count_state"] == "reported" else None}
    state = item.get("status")
    if state in {"inProgress", "running"}:
        result["execution_state"] = "running"
        return gap("native_command_running")
    if state in {"interrupted", "cancelled"}:
        result["execution_state"] = "interrupted"
        return gap("native_command_interrupted")
    code = item.get("exitCode")
    if state == "failed":
        result["execution_state"] = "failure"
    if state not in {"completed", "failed"}:
        return gap("unknown_native_execution_state")
    if not isinstance(code, int) or isinstance(code, bool):
        return gap("missing_native_exit_code" if code is None else "invalid_native_exit_code")
    if state == "failed" and code == 0:
        return gap("conflicting_native_execution_state")
    result.setdefault("original_exit_state", {key: receipt.get(key) for key in ("exit_code", "exit_code_state", "classification")})
    result.update(exit_code=code, exit_code_state="native_captured", execution_state="success" if code == 0 else "failure")
    provenance["reason"] = None
    return result
