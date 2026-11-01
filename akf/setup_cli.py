"""Interactive and structured installation commands; no implicit model calls."""
import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import sys

from .install import (Manager, InstallError, discover_vaults, encoded, environment, git,
                      read_json, sha, snapshot, EVENTS, config_active)

COMMANDS = {"setup", "doctor", "upgrade", "uninstall", "rollback", "managed-hook"}


def add_commands(sub):
    setup = sub.add_parser("setup", help="选择项目、知识库和权限")
    setup.add_argument("--project")
    setup.add_argument("--knowledge")
    setup.add_argument("--discover-in", action="append", default=[])
    setup.add_argument("--sync", choices=["local", "commit", "push"])
    setup.add_argument("--hook-mode", choices=["direct", "plugin"])
    setup.add_argument("--remote")
    for grant in ("update", "commit", "push"):
        setup.add_argument("--authorize-" + grant, choices=["yes", "no"])
    setup.add_argument("--non-interactive", action="store_true")
    setup.add_argument("--apply", action="store_true", help="应用预览；三个授权仍须单独提供")
    setup.add_argument("--allow-overlap", action="store_true")
    setup.add_argument("--expires-at", help="可选授权期限，带时区的 ISO-8601 时间")
    setup.add_argument("--migrate-config", help="明确选择旧配置；仅迁移精确匹配的直接 Hooks")
    doctor = sub.add_parser("doctor", help="只读诊断；--verify 单独保存原生触发验证")
    doctor.add_argument("--verify", action="store_true")
    upgrade = sub.add_parser("upgrade")
    upgrade.add_argument("--from", dest="package", required=True)
    sub.add_parser("uninstall")
    rollback = sub.add_parser("rollback")
    rollback.add_argument("--transaction", required=True)
    hook = sub.add_parser("managed-hook")
    hook.add_argument("--mode", choices=["direct", "plugin"], required=True)


def ask(prompt, default=None):
    value = input(prompt + (f" [{default}]" if default is not None else "") + "：").strip()
    return value or default


def setup(args, manager):
    interactive = not args.non_interactive and sys.stdin.isatty()
    env = environment()
    if interactive:
        print(f"环境：{env['os']} {env['os_version']} / {env['architecture']}；Python {env['python']}；Git {'可用' if env['git'] else '缺失'}；Codex {'已检测' if env['codex_cli'] or env['desktop'] else '缺失'}", file=sys.stderr)
    missing = [key for key in ("project", "knowledge", "sync", "hook_mode", "authorize_update",
                              "authorize_commit", "authorize_push") if getattr(args, key) is None]
    if missing and not interactive:
        raise InstallError("非交互设置需要明确选择：" + ", ".join("--" + k.replace("_", "-") for k in missing))
    if not env["git"] or not (env["codex_cli"] or env["desktop"]):
        raise InstallError("工具安装可保留；请先安装 Git 和 Codex，再从同一安装入口继续设置。官方说明见 doctor。")
    if interactive:
        cwd = git(Path.cwd(), "rev-parse", "--show-toplevel", required=False) or str(Path.cwd())
        for mapping in manager.registry()["mappings"].values():
            if not mapping.get("archived"):
                print(f"已有项目：{mapping['project_root']}；知识库：{mapping['knowledge_root']}", file=sys.stderr)
        args.project = args.project or ask("项目目录", cwd)
        found = discover_vaults(args.discover_in)
        for index, path in enumerate(found, 1):
            print(f"{index}. {path}", file=sys.stderr)
        choice = args.knowledge or ask("知识库目录（可输入上方序号或新目录）")
        args.knowledge = found[int(choice) - 1] if choice and choice.isdigit() and 0 < int(choice) <= len(found) else choice
        if not args.knowledge:
            raise InstallError("尚未选择知识库。重新运行 setup 可继续。")
        args.sync = args.sync or ask("同步方式：local 本地保存 / commit 本地提交 / push 提交并推送", "local")
        args.hook_mode = args.hook_mode or ask("Hook 方式：direct 独立安装 / plugin 已安装插件", "direct")
        if args.sync != "local" and args.remote is None:
            names = git(Path(args.knowledge), "remote", required=False).splitlines()
            print("知识库远端：" + ", ".join(names), file=sys.stderr)
            args.remote = ask("Git 远端名称", names[0] if len(names) == 1 else None)
    grants = {}
    for name, label in (("update", "允许自动更新此知识库"), ("commit", "允许自动创建本地 Git 提交"),
                        ("push", "允许向所选远端自动推送")):
        value = getattr(args, "authorize_" + name)
        if value is None:
            value = ask(label + "（yes/no）", "no")
        if value not in {"yes", "no"}:
            raise InstallError("授权请输入 yes 或 no。")
        grants[name] = value == "yes"
    with manager.lock():
        preview = manager.setup(args.project, args.knowledge, args.sync, args.hook_mode, grants,
                                args.remote, args.allow_overlap, apply=False, expires_at=args.expires_at,
                                legacy_config=args.migrate_config)
        if interactive and preview["state"] != "no_op":
            for key, label in (("project_read", "项目读取"), ("knowledge_write", "知识写入"),
                               ("state_directory", "状态目录"), ("hook_source", "Hook 来源"), ("git_remote", "Git 目标")):
                print(f"{label}：{preview.get(key) or '无'}", file=sys.stderr)
            print("权限：自动更新 " + ("开启" if grants["update"] else "关闭") + "；本地提交 " +
                  ("开启" if grants["commit"] else "关闭") + "；远端推送 " + ("开启" if grants["push"] else "关闭"), file=sys.stderr)
        else:
            print(json.dumps(preview, ensure_ascii=False, indent=2), file=sys.stderr)
        apply = args.apply if not interactive else ask("应用以上改动（yes/no）", "no") == "yes"
        if not apply:
            return {"state": "preview", "preview": preview, "next_step": "确认后使用同样选择并添加 --apply。"}
        return manager.setup(args.project, args.knowledge, args.sync, args.hook_mode, grants,
                             args.remote, args.allow_overlap, expected=preview["preview_id"], expires_at=args.expires_at,
                             legacy_config=args.migrate_config)


def inspect(manager, mapping=None, verify=False):
    registry = manager.registry()
    installed = registry.get("installed", False)
    errors = []
    if installed:
        manifest = read_json(Path(registry["package"]) / "PACKAGE.json", {})
        for name, record in manifest.get("files", {}).items():
            path = Path(registry["package"]) / name
            if not path.is_file() or path.is_symlink() or sha(path.read_bytes()) != record["sha256"]:
                errors.append("安装文件校验失败：" + name)
        if (not manifest.get("files") or not manager.bin.is_file() or manager.bin.is_symlink()
                or not os.access(manager.bin, os.X_OK) or sha(manager.bin.read_bytes()) != registry.get("launcher_sha256")):
            errors.append("启动入口或安装清单不完整，请重新运行安装包。")
        pointer = manager.code / "current.json"
        if not pointer.is_file() or pointer.is_symlink() or sha(pointer.read_bytes()) != registry.get("current_sha256"):
            errors.append("版本入口发生变化，请检查事务恢复记录。")
    result = {"states": {"Installed": installed and not errors, "Configured": False, "Trusted": "待确认",
                         "Enabled": False, "Sync Enabled": {"commit": False, "push": False}},
              "environment": environment(), "errors": errors,
              "mappings": [{k: m[k] for k in ("id", "project_root", "knowledge_root", "hook_mode")}
                           for m in registry["mappings"].values() if not m.get("archived")],
              "next_step": "运行 akf setup 选择项目和知识库。"}
    result["transactions"] = [{"id": t["id"], "state": t["state"], "action": t["action"], "recovery": t["recovery"]}
                              for p in sorted((manager.home / "transactions").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]
                              for t in [read_json(p)] if t["state"] in {"prepared", "interrupted", "rollback_conflict"}]
    if not mapping:
        return result
    result["mapping"] = mapping["id"]
    config = manager.config(mapping)
    configured = bool(config) and all(Path(config[k]).is_dir() for k in ("project_root", "knowledge_root"))
    result["states"]["Configured"] = configured
    result["authorization"] = mapping["grants"]
    result["control"] = {k: mapping[k] for k in ("paused", "disabled")}
    result["next_step"] = "在 Codex /hooks 审查当前定义；执行普通任务后运行 akf doctor --verify。"
    if not configured or not installed:
        return result
    from .native import NativeClient, hook_status
    try:
        with NativeClient() as client:
            native = hook_status(client, manager, mapping)
            result["states"]["Trusted"] = native["state"]
            if verify and native["trusted"] and native["active"]:
                since = max(Path(mapping["config_path"]).stat().st_mtime, (manager.code / "current.json").stat().st_mtime)
                proof = verify_job(client, mapping, since)
                if proof:
                    with manager.lock():
                        latest = manager.registry()
                        item = latest["mappings"][mapping["id"]]
                        item.update(verified_package=registry["package_id"], verified_hooks=native["hashes"],
                                    verified_since=since, native_proof=proof)
                        manager.transaction([(manager.registry_path, encoded(latest), 0o600)], "verify_native_trigger")
                        mapping = item
            enabled = (not errors and native["trusted"] and native["active"] and mapping["grants"]["update"]
                       and config_active(config) and not mapping["paused"] and not mapping["disabled"]
                       and mapping.get("verified_package") == registry["package_id"]
                       and mapping.get("verified_since") == max(Path(mapping["config_path"]).stat().st_mtime,
                                                                (manager.code / "current.json").stat().st_mtime)
                       and mapping.get("verified_hooks") == native["hashes"])
            result["states"]["Enabled"] = bool(enabled)
            result["verification"] = "通过" if enabled else "待验证"
            if enabled:
                result["next_step"] = "自动知识维护已启用；使用 akf pause 可暂停。"
            if mapping["grants"]["commit"]:
                from .git_sync import _verify_binding, SyncError
                try:
                    _verify_binding(Path(config["knowledge_root"]), config["git"])
                    result["states"]["Sync Enabled"] = {"commit": bool(enabled), "push": bool(enabled and mapping["grants"]["push"])}
                except SyncError:
                    result["errors"].append("Git 绑定检查未通过；自动同步待确认。")
    except (InstallError, OSError, ValueError, KeyError) as error:
        result["next_step"] = str(error) if isinstance(error, InstallError) else "无法确认原生状态。打开 Codex，在 /hooks 审查后重新运行 doctor。"
    return result


def verify_job(client, mapping, since=0):
    """Bind a terminal job to native Hook context and its originating task."""
    config = read_json(Path(mapping["config_path"]))
    db = Path(config["state_dir"]) / "state.sqlite3"
    if not db.is_file():
        return None
    with contextlib.closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as connection:
        rows = connection.execute("SELECT value FROM state WHERE kind='job' ORDER BY rowid DESC LIMIT 5").fetchall()
    for (raw,) in rows:
        job = json.loads(raw)
        if dt.datetime.fromisoformat(job["created_at"].replace("Z", "+00:00")).timestamp() < since:
            continue
        outcome = "write"
        if job.get("state") == "no_change":
            if any(job.get(key) for key in ("pending_updates", "error", "plans", "write_state", "write_history",
                                            "write_id", "commit", "commit_parent", "stage_owned", "push_attempted",
                                            "remote_head", "undo_job")):
                continue
            project = Path(config["project_root"])
            knowledge = Path(config["knowledge_root"])
            observed = [(project, source["path"], source["sha256"]) for source in job.get("source_files", [])]
            for update in job.get("candidate", {}).get("updates", []):
                observed.append((knowledge, update["topic"], update.get("expected_sha256")))
                observed.extend((project, source["path"], source["sha256"])
                                for source in update.get("evidence", []) if source.get("kind") == "file")
            unchanged = True
            for root, relative, expected in observed:
                path = root / relative
                if (not path.resolve().is_relative_to(root.resolve())
                        or any(p.is_symlink() for p in [path, *path.parents] if p.is_relative_to(root))
                        or path.exists() and not path.is_file()
                        or (sha(path.read_bytes()) if path.is_file() else None) != expected):
                    unchanged = False
                    break
            if not unchanged:
                continue
            outcome = "no_change"
        else:
            if job.get("write_state") not in {"local_saved", "committed", "remote_confirmed"}:
                continue
            if not job.get("plans") or not all(sha((Path(config["knowledge_root"]) / p["topic"]).read_bytes()) == p["after_sha256"] for p in job["plans"]):
                continue
        turn_id = job.get("maintenance_turn_id") or job.get("turn_id")
        if not turn_id:
            continue
        prefinal = job.get("preparation_source") == "prefinal"
        if prefinal:
            marker = job.get("trigger_marker", "")
            tool_id = job.get("trigger_tool_use_id")
            receipt = next((r for r in job.get("receipts", [])
                            if r.get("id") == job.get("trigger_receipt_id")), None)
            if (not re.fullmatch(r"\[AKF-PREFINAL:[0-9a-f]{32}\]", marker)
                    or job.get("trigger_event") != "PostToolUse" or turn_id != job.get("turn_id")
                    or not tool_id or not receipt or receipt.get("tool_use_id") != tool_id
                    or receipt.get("session_id") != job["session_id"] or receipt.get("turn_id") != turn_id
                    or receipt["id"] != sha((job["session_id"] + ":" + turn_id + ":" + tool_id).encode())[:24]):
                continue
            trigger_time = dt.datetime.fromisoformat(receipt["observed_at"].replace("Z", "+00:00")).timestamp()
            prepared_time = dt.datetime.fromisoformat(job["created_at"].replace("Z", "+00:00")).timestamp()
            thread = client.request("thread/read", {"threadId": job["session_id"], "includeTurns": False}).get("thread", {})
            if (thread.get("id") != job["session_id"] or not thread.get("path") or not thread.get("cwd")
                    or not Path(thread["cwd"]).resolve().is_relative_to(Path(config["project_root"]).resolve())):
                continue
            rollout = Path(thread["path"])
            sessions = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().absolute() / "sessions"
            if (not rollout.is_absolute() or not rollout.is_relative_to(sessions) or not rollout.is_file()
                    or not rollout.resolve().is_relative_to(sessions.resolve())
                    or any(p.is_symlink() for p in (rollout, *rollout.parents) if p.is_relative_to(sessions))):
                continue
            with rollout.open("rb") as stream:
                offset = max(0, stream.seek(0, 2) - 4_194_304)
                stream.seek(offset)
                tail = stream.read(4_194_304)
            lines = tail.splitlines()[1:] if offset else tail.splitlines()
            hidden_context = False
            for line in lines:
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                    continue
                payload = record.get("payload", {})
                metadata = payload.get("internal_chat_message_metadata_passthrough", {})
                if not isinstance(metadata, dict):
                    continue
                context_time = metadata.get("create_time")
                if (record.get("type") != "response_item" or payload.get("type") != "message"
                        or payload.get("role") != "developer" or metadata.get("turn_id") != turn_id
                        or isinstance(context_time, bool) or not isinstance(context_time, (int, float))
                        or not max(since, trigger_time) <= context_time <= prepared_time):
                    continue
                for content, kind, details in zip(payload.get("content", []), metadata.get("content_item_kinds", []),
                                                   metadata.get("content_item_metadata", [])):
                    if not isinstance(content, dict) or not isinstance(details, dict):
                        continue
                    provenance = details.get("provenance", {})
                    if (kind == "hooks.additional_context" and isinstance(provenance, dict) and provenance.get("type") == "harness"
                            and provenance.get("hook") is True and content.get("type") == "input_text"
                            and isinstance(content.get("text"), str) and content["text"].startswith(marker + "\n")):
                        hidden_context = True
                        break
                if hidden_context:
                    break
            if not hidden_context:
                continue
        params = {"threadId": job["session_id"], "turnId": turn_id,
                  "limit": 20, "sortDirection": "desc"}
        while True:
            data = client.request("thread/items/list", params)
            for entry in data.get("data", []):
                if entry.get("turnId", turn_id) != turn_id:
                    continue
                item = entry["item"]
                if prefinal:
                    if item.get("id") != tool_id:
                        continue
                    native_type = item.get("type")
                    matches = native_type in {"subAgentActivity", "collabAgentToolCall", "fileChange", "imageView"}
                    if receipt.get("tool_name") == "Bash":
                        command = item.get("command", "")
                        try:
                            words = shlex.split(command)
                        except ValueError:
                            words = []
                        if (len(words) == 3 and Path(words[0]).name in {"sh", "bash", "zsh", "dash", "ksh", "fish"}
                                and words[1] in {"-c", "-lc"}):
                            command = words[2]
                        matches = native_type == "commandExecution" and sha(command.encode()) == receipt.get("command_sha256")
                    elif str(receipt.get("tool_name", "")).startswith("mcp__"):
                        matches = (native_type == "mcpToolCall" and receipt["tool_name"] ==
                                   "mcp__" + item.get("server", "") + "__" + item.get("tool", ""))
                    if matches:
                        return {"job": job["id"], "session": job["session_id"], "outcome": outcome,
                                "native_hook_context": marker, "native_tool_use_id": tool_id, "source": "prefinal"}
                    continue
                if item.get("type") != "hookPrompt":
                    continue
                for fragment in item["fragments"]:
                    if fragment["text"].startswith(job["prompt_marker"] + "\n") and fragment.get("hookRunId"):
                        return {"job": job["id"], "session": job["session_id"],
                                "native_hook_run": fragment["hookRunId"], "outcome": outcome}
            if data.get("nextCursor") is None:
                break
            params = {**params, "cursor": data["nextCursor"]}
    return None


def managed_hook(args, manager):
    # A disabled, unmatched or unconfigured plugin does not create business state.
    raw = sys.stdin.buffer.read(1_048_577)
    if len(raw) > 1_048_576:
        raise InstallError("Hook 输入过大。")
    event = json.loads(raw)
    if not isinstance(event, dict) or not isinstance(event.get("cwd"), str):
        return {}
    if event.get("hook_event_name") not in EVENTS:
        return {}
    mapping = manager.select(event["cwd"])
    registry = manager.registry()
    if (not registry.get("installed") or not mapping or mapping["hook_mode"] != args.mode
            or mapping["paused"] or mapping["disabled"] or not mapping["grants"]["update"]):
        return {}
    with manager.lock():
        mapping = manager.select(event["cwd"])
        if (not manager.registry().get("installed") or not mapping or mapping["hook_mode"] != args.mode
                or mapping["paused"] or mapping["disabled"] or not mapping["grants"]["update"]):
            return {}
        config = manager.config(mapping)
        if not config_active(config):
            return {}
        from .engine import Engine
        from .hooks import handle_event
        engine = Engine(mapping["config_path"])
        try:
            return handle_event(event, engine)
        finally:
            engine.db.close()


def run(args):
    manager = Manager()
    if args.command == "setup":
        return setup(args, manager)
    if args.command == "managed-hook":
        return managed_hook(args, manager)
    if args.command in {"doctor", "status"}:
        try:
            mapping = manager.select(identity=args.mapping)
        except InstallError as error:
            result = inspect(manager)
            result["errors"].append(str(error))
            result["next_step"] = "使用 akf --mapping ID doctor 明确选择项目。"
            return result
        return inspect(manager, mapping, getattr(args, "verify", False))
    with manager.lock():
        if args.command == "upgrade":
            return manager.install_package(args.package)
        if args.command == "rollback":
            return manager.rollback(args.transaction)
        if args.command == "uninstall":
            return manager.uninstall()
        mapping = manager.select(identity=args.mapping)
        if not mapping:
            raise InstallError("当前项目尚未设置。请运行 akf setup。")
        return manager.control(mapping["id"], args.command)
