"""Local installation ownership, reversible changes and project bindings.

This layer never imports Engine. Notes and business Git are outside transactions.
"""
from __future__ import annotations

import base64
import ast
import contextlib
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import tomllib
import uuid
from itertools import islice

EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "Interrupt")


class InstallError(Exception):
    pass


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def config_active(config):
    return bool(config.get("enabled")) and (not config.get("expires_at") or
           dt.datetime.fromisoformat(config["expires_at"].replace("Z", "+00:00")) > dt.datetime.now(dt.timezone.utc))


def read_json(path, default=None):
    if not path.exists():
        return copy.deepcopy(default)
    try:
        if path.stat().st_size > 4_194_304:
            raise ValueError()
        return json.loads(path.read_bytes())
    except (ValueError, UnicodeError):
        raise InstallError(f"配置无法解析，已保留原文件：{path}") from None


def write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".akf-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def snapshot(path):
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise InstallError(f"路径含符号链接，请选择实际目录：{path}")
    if not path.exists():
        return None
    if not path.is_file():
        raise InstallError(f"目标不是普通文件，已保留：{path}")
    return {"data": base64.b64encode(path.read_bytes()).decode(), "mode": stat.S_IMODE(path.stat().st_mode)}


def restore(path, value):
    if value is None:
        path.unlink(missing_ok=True)
    else:
        write(path, base64.b64decode(value["data"]), value["mode"])


def git(root, *args, required=True):
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10,
                                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.TimeoutExpired):
        raise InstallError("Git 不可用。请安装 Git 后重新运行此入口。") from None
    if result.returncode and required:
        raise InstallError("Git 绑定检查失败。请确认知识库仓库、分支和远端。")
    return result.stdout.strip() if not result.returncode else ""


def environment():
    result = {"os": platform.system(), "os_version": platform.mac_ver()[0] or platform.release(),
              "architecture": platform.machine(), "python": platform.python_version(),
              "python_supported": sys.version_info >= (3, 11), "git": None, "codex_cli": None,
              "desktop": None, "platform_status": "验收目标" if sys.platform == "darwin" else "未验收"}
    for key, command in (("git", ["git", "--version"]), ("codex_cli", ["codex", "--version"])):
        if shutil.which(command[0]):
            try:
                r = subprocess.run(command, capture_output=True, text=True, timeout=5)
                result[key] = r.stdout.strip() if r.returncode == 0 else None
            except subprocess.TimeoutExpired:
                pass
    if sys.platform == "darwin":
        import plistlib
        for folder in (Path("/Applications"), Path.home() / "Applications"):
            for name in ("Codex.app", "ChatGPT.app"):
                info = folder / name / "Contents/Info.plist"
                if info.is_file():
                    with info.open("rb") as stream:
                        data = plistlib.load(stream)
                    result["desktop"] = {"app": name, "version": data.get("CFBundleShortVersionString"),
                                         "build": data.get("CFBundleVersion")}
                    break
    result["next_steps"] = []
    for key, url in (("git", "https://git-scm.com/download/mac"),
                     ("codex_cli", "https://learn.chatgpt.com/docs/cli")):
        if not result[key]:
            result["next_steps"].append(f"安装 {key} 后重新运行 akf setup：{url}")
    return result


def discover_vaults(scopes=()):
    """Read registrations and directory names only; never open note bodies."""
    found = set()
    registration = Path.home() / "Library/Application Support/obsidian/obsidian.json"
    if registration.is_file():
        meta = read_json(registration, {})
        if not isinstance(meta, dict) or not isinstance(meta.get("vaults", {}), dict):
            raise InstallError("Obsidian 注册信息无法解析。请直接输入知识库目录。")
        for item in meta.get("vaults", {}).values():
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                p = Path(item["path"]).expanduser().resolve()
                if p.is_dir():
                    found.add(str(p))
    for scope in scopes:
        p = Path(scope).expanduser().resolve(strict=True)
        if not p.is_dir():
            raise InstallError("知识库发现范围必须是目录。")
        candidates = [p, *islice(p.iterdir(), 200)]
        for child in candidates:
            if child.is_dir() and not child.is_symlink():
                if (child / ".obsidian").is_dir() or any(child.glob("*.md")):
                    found.add(str(child))
    return sorted(found)


def hooks_document(path):
    doc = read_json(path, {})
    if not isinstance(doc, dict) or not isinstance(doc.get("hooks", {}), dict):
        raise InstallError(f"Hook 配置结构有冲突，已保留：{path}")
    for groups in doc.get("hooks", {}).values():
        if not isinstance(groups, list) or any(not isinstance(g, dict) or not isinstance(g.get("hooks"), list)
                                               or any(not isinstance(h, dict) for h in g["hooks"]) for g in groups):
            raise InstallError(f"Hook handler 无法安全合并，已保留：{path}")
        if any(h.get("type") == "command" and not isinstance(h.get("command"), str) for g in groups for h in g["hooks"]):
            raise InstallError(f"Hook command 格式无效，已保留：{path}")
    return doc


def has_akf_command(hooks):
    if not isinstance(hooks, dict):
        return False
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                continue
            for handler in group["hooks"]:
                if not isinstance(handler, dict) or handler.get("type", "command") != "command":
                    continue
                for field in ("command", "commandWindows", "command_windows"):
                    command = handler.get(field)
                    if isinstance(command, str) and re.search(r"(?:\bakf\b|agent-knowledge-workflow)", command, re.I):
                        return True
    return False


def undo_json(before, after, current):
    """Undo only this transaction's JSON delta, retaining later unrelated edits."""
    if current == after:
        return before
    if isinstance(before, dict) and isinstance(after, dict) and isinstance(current, dict):
        value = copy.deepcopy(current)
        for key in before.keys() | after.keys():
            if before.get(key) == after.get(key) and (key in before) == (key in after):
                continue
            if key not in current:
                if key not in before:
                    continue
                raise InstallError("配置字段已修改；请检查事务恢复材料。")
            if key not in before:
                if value[key] != after[key]:
                    if isinstance(after[key], (dict, list)):
                        merged = undo_json(type(after[key])(), after[key], value[key])
                        if merged:
                            value[key] = merged
                            continue
                    else:
                        raise InstallError("新增配置字段已修改；请检查事务恢复材料。")
                del value[key]
            elif key not in after:
                raise InstallError("已删除字段被重新创建；请检查事务恢复材料。")
            else:
                value[key] = undo_json(before[key], after[key], current[key])
        return value
    if isinstance(before, list) and isinstance(after, list) and isinstance(current, list):
        # Shared Hook arrays are append-only. Existing groups are never rewritten.
        if after[:len(before)] == before:
            value = copy.deepcopy(current)
            for item in after[len(before):]:
                if value.count(item) != 1:
                    raise InstallError("AKF Hook 条目已修改；请检查事务恢复材料。")
                value.remove(item)
            return value
        if before[:len(after)] == after:
            value = copy.deepcopy(current)
            for item in before[len(after):]:
                if item not in value:
                    value.append(item)
            return value
        added = [item for item in after if item not in before]
        removed = [item for item in before if item not in after]
        if any(before.count(x) > 1 or after.count(x) > 1 for x in before + after):
            raise InstallError("共享配置存在重复条目，请检查事务恢复材料。")
        value = copy.deepcopy(current)
        for item in added:
            if value.count(item) != 1:
                raise InstallError("工具条目已改变，请检查事务恢复材料。")
            value.remove(item)
        for item in removed:
            if item not in value:
                value.insert(min(before.index(item), len(value)), item)
        return value
    raise InstallError("配置与安装快照不同，已保留当前文件。请检查事务恢复材料。")


class Manager:
    def __init__(self, data_home=None):
        home = Path.home()
        default = home / ("Library/Application Support/AgentKnowledgeWorkflow" if sys.platform == "darwin"
                          else ".local/state/agent-knowledge-workflow")
        self.home = Path(data_home or os.environ.get("AKF_HOME", default)).expanduser().absolute()
        self.code = Path(os.environ.get("AKF_INSTALL_ROOT", home / ".local/share/agent-knowledge-workflow")).expanduser().absolute()
        self.bin = Path(os.environ.get("AKF_BIN_DIR", home / ".local/bin")).expanduser().absolute() / "akf"
        self.codex = Path(os.environ.get("CODEX_HOME", home / ".codex")).expanduser().absolute()
        self.registry_path = self.home / "installation.json"

    def registry(self):
        data = read_json(self.registry_path, {"schema": 1, "mappings": {}, "installed": False})
        if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("mappings"), dict):
            raise InstallError("安装记录格式无法识别，请保留记录并运行 doctor。")
        required = {"id", "project_root", "knowledge_root", "config_path", "hook_mode", "sync", "grants", "paused", "disabled", "config_sha256"}
        if any(not isinstance(m, dict) or not required <= m.keys() for m in data["mappings"].values()):
            raise InstallError("项目映射不完整，已保留安装记录。请检查事务恢复材料。")
        return data

    def config(self, mapping):
        path = Path(mapping["config_path"])
        value = read_json(path)
        if value is None or sha(path.read_bytes()) != mapping["config_sha256"]:
            raise InstallError("映射配置已在安装器外改变。请保留文件并通过 setup 建立新绑定。")
        return value

    @contextlib.contextmanager
    def lock(self):
        if sys.platform not in {"darwin", "linux"}:
            raise InstallError("此安装管理层尚未在当前操作系统验收。请使用已验证的 macOS。")
        import fcntl
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.home, os.O_RDONLY)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise InstallError("AKF 正在执行操作。请等待完成后重试。") from None
            yield
        finally:
            os.close(fd)

    def transaction(self, changes, action, directories=()):
        ops = []
        for path, data, mode in changes:
            path = Path(path).absolute()
            before = snapshot(path)
            after = None if data is None else {"data": base64.b64encode(data).decode(), "mode": mode}
            if before != after:
                ops.append({"path": str(path), "before": before, "after": after,
                            "before_sha256": sha(base64.b64decode(before["data"])) if before else None,
                            "after_sha256": sha(data) if data is not None else None})
        if not ops:
            return {"state": "no_op"}
        identity = uuid.uuid4().hex
        journal = self.home / "transactions" / (identity + ".json")
        created = set()
        for directory in [*(Path(p) for p in directories), *(Path(op["path"]).parent for op in ops)]:
            for candidate in [directory, *directory.parents]:
                if not candidate.exists():
                    created.add(str(candidate))
        record = {"id": identity, "action": action, "state": "prepared", "operations": ops,
                  "created_directories": sorted(created, key=lambda p: len(Path(p).parts)),
                  "native_actions": [], "recovery": "akf rollback --transaction " + identity}
        write(journal, encoded(record))
        try:
            for directory in record["created_directories"]:
                Path(directory).mkdir(exist_ok=True, mode=0o700)
            for op in ops:
                path = Path(op["path"])
                if snapshot(path) != op["before"]:
                    raise InstallError("预览后文件已变化，停止应用。请重新运行设置。")
                restore(path, op["after"])
            record["state"] = "applied"
            write(journal, encoded(record))
        except BaseException:
            record["state"] = "interrupted"
            write(journal, encoded(record))
            self.rollback(identity)
            raise
        return {"state": "applied", "transaction": identity}

    def rollback(self, identity):
        if not re.fullmatch(r"[a-f0-9]{32}", identity):
            raise InstallError("事务 ID 无效。请使用安装结果中的 transaction。")
        path = self.home / "transactions" / (identity + ".json")
        record = read_json(path)
        if not record:
            raise InstallError("找不到事务。请确认 AKF_HOME 和事务 ID。")
        if record["state"] == "rolled_back":
            return {"state": "no_op", "transaction": identity}
        conflicts, changes = [], []
        for op in reversed(record["operations"]):
            target = Path(op["path"])
            current = snapshot(target)
            if current == op["before"]:
                continue
            if current == op["after"]:
                changes.append((target, op["before"]))
                continue
            try:
                old = json.loads(base64.b64decode(op["before"]["data"])) if op["before"] else {}
                new = json.loads(base64.b64decode(op["after"]["data"])) if op["after"] else {}
                present = json.loads(base64.b64decode(current["data"]))
                merged = undo_json(old, new, present)
                if target == self.registry_path:
                    required = {"id", "project_root", "knowledge_root", "config_path", "hook_mode", "sync", "grants", "paused", "disabled", "config_sha256"}
                    if merged.get("schema") != 1 or not isinstance(merged.get("mappings"), dict) or any(
                            not required <= m.keys() for m in merged["mappings"].values()):
                        raise InstallError("后续安装事务依赖此记录。请先回滚后续事务。")
                changes.append((target, {"data": base64.b64encode(encoded(merged)).decode(), "mode": current["mode"]}))
            except (InstallError, ValueError, TypeError, KeyError):
                conflicts.append(str(target))
        if not conflicts:
            for target, value in changes:
                restore(target, value)
            for directory in reversed(record.get("created_directories", [])):
                try:
                    Path(directory).rmdir()
                except (FileNotFoundError, OSError):
                    pass
        record["state"] = "rollback_conflict" if conflicts else "rolled_back"
        record["conflicts"] = conflicts
        write(path, encoded(record))
        return {"state": record["state"], "transaction": identity, "conflicts": conflicts,
                "next_step": "检查事务备份中的冲突文件后重试。" if conflicts else "恢复完成；原生 Hook 信任需要重新确认。"}

    def launcher(self):
        bootstrap = ("import json,os,sys; from pathlib import Path; "
                     f"os.environ.setdefault('AKF_HOME', {str(self.home)!r}); "
                     f"os.environ.setdefault('AKF_INSTALL_ROOT', {str(self.code)!r}); "
                     f"os.environ.setdefault('AKF_BIN_DIR', {str(self.bin.parent)!r}); "
                     f"p=Path({str(self.code / 'current.json')!r}); "
                     "d=json.loads(p.read_bytes()); sys.path.insert(0,d['path']); "
                     "from akf.cli import main; raise SystemExit(main())")
        return ("#!/bin/sh\n# AKF managed launcher v1\nexec " + shlex.join([sys.executable, "-B", "-c", bootstrap]) + ' "$@"\n').encode()

    def install_package(self, source):
        source = Path(source).expanduser().resolve(strict=True)
        manifest = read_json(source / "PACKAGE.json")
        if not isinstance(manifest, dict) or manifest.get("name") != "agent-knowledge-workflow":
            raise InstallError("请选择解压后的 AKF 私有安装包目录。")
        files = manifest.get("files", {})
        if (not isinstance(files, dict) or not {"akf/__main__.py", "akf/cli.py", "akf/install.py"} <= files.keys()
                or len({"plugin.json", ".codex-plugin/plugin.json"} & files.keys()) != 1):
            raise InstallError("安装包缺少入口，请重新获取完整安装包。")
        payload = {}
        for name, expected in files.items():
            if not isinstance(expected, dict) or not isinstance(expected.get("sha256"), str):
                raise InstallError("安装包校验清单格式无效。")
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts or any(c in name for c in "\n\r\0"):
                raise InstallError("安装包路径无效。")
            path = source / relative
            actual = snapshot(path)
            if actual is None or sha(base64.b64decode(actual["data"])) != expected["sha256"]:
                raise InstallError("安装包内容校验失败，请重新获取完整安装包。")
            payload[name] = base64.b64decode(actual["data"])
            if name.endswith(".py"):
                try:
                    ast.parse(payload[name], filename=name)
                except (SyntaxError, ValueError):
                    raise InstallError("安装包 Python 自检失败，保持现有版本。") from None
        identity = sha(encoded(manifest))[:16]
        version = manifest.get("version", "")
        if not isinstance(version, str) or not re.fullmatch(r"[0-9A-Za-z._-]{1,50}", version):
            raise InstallError("安装包版本格式无效。")
        destination = self.code / "versions" / (version + "-" + identity)
        existing = self.registry()
        pointer = self.code / "current.json"
        if pointer.exists() and (not existing.get("current_sha256") or sha(pointer.read_bytes()) != existing["current_sha256"]):
            raise InstallError("版本入口已改变，已保留。请检查 current.json 的归属及恢复记录。")
        launcher = self.launcher()
        if self.bin.exists() and self.bin.read_bytes() != launcher:
            if not existing.get("launcher_sha256") or sha(self.bin.read_bytes()) != existing["launcher_sha256"]:
                raise InstallError(f"已有同名入口，已保留：{self.bin}。请设置 AKF_BIN_DIR 选择专用目录。")
        changes = []
        for name, data in payload.items():
            target = destination / name
            if target.exists() and target.read_bytes() != data:
                raise InstallError("已安装版本被修改，请保留现场并运行 doctor。")
            mode = files[name].get("mode", 0o644)
            if mode not in {0o644, 0o755}:
                raise InstallError("安装包文件权限无效。")
            changes.append((target, data, mode))
        changes.append((destination / "PACKAGE.json", encoded(manifest), 0o644))
        if not existing.get("installed"):
            for mapping in existing["mappings"].values():
                if not mapping.get("archived") and mapping.get("owned_hooks"):
                    mapping["hooks_uninstalled"] = True
        existing.update(installed=True, version=version, package_id=identity, package=str(destination),
                        launcher=str(self.bin), launcher_sha256=sha(launcher))
        current = encoded({"path": str(destination), "version": version})
        existing["current_sha256"] = sha(current)
        # A version switch invalidates native verification; configuration and pending jobs stay.
        for mapping in existing["mappings"].values():
            if mapping.get("verified_package") != identity:
                mapping.pop("verified_package", None)
        changes.extend([(self.code / "current.json", current, 0o600),
                        (self.bin, launcher, 0o755), (self.registry_path, encoded(existing), 0o600)])
        return {**self.transaction(changes, "install_or_upgrade"), "version": version, "entrypoint": str(self.bin)}

    def select(self, cwd=None, identity=None):
        registry = self.registry()
        if identity:
            mapping = registry["mappings"].get(identity)
            if mapping and not mapping.get("archived"):
                return mapping
            raise InstallError("映射不存在或已归档。请运行 akf setup。")
        cwd = Path(cwd or Path.cwd()).resolve()
        matches = [m for m in registry["mappings"].values() if not m.get("archived")
                   and cwd.is_relative_to(Path(m["project_root"]))]
        if len(matches) > 1:
            raise InstallError("当前目录匹配多个项目。请用 --mapping ID 明确选择。")
        return matches[0] if matches else None

    def hook_group(self):
        command = shlex.join([str(self.bin), "managed-hook", "--mode", "direct"])
        return {name: {"hooks": [{"type": "command", "command": command,
                                  "timeout": 3 if name == "Interrupt" else 10}]} for name in EVENTS}

    def ownership_seed(self, config, mappings, current_id):
        """Carry verified fact ownership only; execution and authorization stay in their original state."""
        project, knowledge = (Path(config[k]).resolve() for k in ("project_root", "knowledge_root"))
        sources = [m for m in mappings.values() if Path(m["project_root"]).resolve() == project
                   and Path(m["knowledge_root"]).resolve() == knowledge]
        sources.sort(key=lambda m: m["id"] != current_id)
        report = {"fact_count": 0, "sources": []}
        if not sources:
            return None, report
        from .engine import binding_fingerprint, timestamp, WorkflowError
        selected = {}
        try:
            for mapping in sources:
                previous = self.config(mapping)
                paths = {}
                for key in ("project_root", "knowledge_root", "state_dir"):
                    value = Path(previous[key]).expanduser()
                    paths[key] = (value if value.is_absolute() else Path(mapping["config_path"]).parent / value).resolve()
                if (paths["project_root"] != project or paths["knowledge_root"] != knowledge
                        or previous.get("environment", "local") != config.get("environment", "local")):
                    continue
                database = paths["state_dir"] / "state.sqlite3"
                if not database.exists():
                    continue
                raw_state = Path(previous["state_dir"]).expanduser()
                if not raw_state.is_absolute():
                    raw_state = Path(mapping["config_path"]).parent / raw_state
                if any(p.is_symlink() for p in [raw_state / "state.sqlite3", raw_state, *raw_state.parents]):
                    raise InstallError("旧知识状态路径含符号链接，请先检查归属。")
                with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                    connection.execute("BEGIN")
                    binding = connection.execute("SELECT value FROM state WHERE kind='meta' AND key='binding'").fetchone()
                    expected = binding_fingerprint(previous, paths["project_root"], paths["knowledge_root"], paths["state_dir"])
                    if binding is None:
                        if mapping.get("ownership_seed_sha256") != sha(database.read_bytes()):
                            raise InstallError("旧知识状态缺少绑定证明，请先检查恢复记录。")
                    elif json.loads(binding[0]) != expected:
                        raise InstallError("旧知识状态绑定不匹配，请先检查原配置。")
                    for key, raw in connection.execute("SELECT key,value FROM state WHERE kind='fact'"):
                        fact = json.loads(raw)
                        fields = ("topic", "claim_id", "title", "content", "observed_at", "block")
                        if (not isinstance(fact, dict) or any(not isinstance(fact.get(k), str) for k in fields)
                                or not isinstance(fact.get("history"), list) or not isinstance(fact.get("evidence"), list)):
                            raise InstallError("旧事实记录不完整，请先检查原状态。")
                        topic, claim, block = fact["topic"], fact["claim_id"], fact["block"]
                        relative = Path(topic)
                        if (not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", claim) or key != topic + ":" + claim
                                or not relative.parts or relative.is_absolute() or relative.as_posix() != topic
                                or relative.parts[0] != "topics" or relative.suffix != ".md"
                                or any(p.startswith(".") for p in relative.parts) or any(ord(c) < 32 for c in topic)
                                or not block.startswith("<!-- akf:" + claim + " -->\n")
                                or not block.endswith("<!-- /akf:" + claim + " -->")):
                            raise InstallError("旧事实身份或主题路径无效，请先检查归属。")
                        rank = (mapping["id"] == current_id, timestamp(fact["observed_at"]))
                        prior = selected.get(key)
                        if prior and prior[0] == rank and prior[1] != fact:
                            raise InstallError("旧事实来源存在同时间冲突，请先人工核对。")
                        if prior is None or rank > prior[0]:
                            selected[key] = (rank, fact, mapping["id"])
        except (sqlite3.Error, ValueError, TypeError, KeyError, WorkflowError):
            raise InstallError("旧知识状态无法验证，原配置和数据已保留。") from None
        if not selected:
            return None, report
        notes = {}
        for _, fact, _ in selected.values():
            topic = fact["topic"]
            if topic not in notes:
                note = knowledge / topic
                if (any(p.is_symlink() for p in [note, *note.parents] if p.is_relative_to(knowledge))
                        or not note.resolve().is_relative_to(knowledge) or not note.is_file()):
                    raise InstallError("已有知识主题缺失或路径已改变，请先人工核对。")
                notes[topic] = note.read_text()
            if notes[topic].count(fact["block"]) != 1:
                raise InstallError("已有知识块已改动或重复，请先人工核对；保留原安装。")
        with contextlib.closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("CREATE TABLE state (kind TEXT, key TEXT, value TEXT, PRIMARY KEY(kind,key))")
            connection.executemany("INSERT INTO state VALUES ('fact',?,?)",
                                   [(key, json.dumps(selected[key][1], ensure_ascii=False, sort_keys=True)) for key in sorted(selected)])
            connection.commit()
            data = connection.serialize()
        report.update(fact_count=len(selected), sources=sorted({item[2] for item in selected.values()}))
        return data, report

    def setup(self, project, knowledge, sync, mode, grants, remote=None, overlap=False, *, apply=True, expected=None, expires_at=None, legacy_config=None):
        if not self.registry().get("installed"):
            raise InstallError("请先从安装包运行 Install.command。")
        project = Path(project).expanduser().resolve(strict=True)
        knowledge = Path(knowledge).expanduser().resolve()
        if (not project.is_dir() or project == knowledge or knowledge.is_relative_to(self.home)
                or self.home.is_relative_to(knowledge) or knowledge.is_relative_to(self.code)
                or self.code.is_relative_to(knowledge)):
            raise InstallError("请选择独立的项目目录和知识库目录。")
        if any(p.is_symlink() for p in [knowledge, *knowledge.parents]) or (knowledge.exists() and not knowledge.is_dir()):
            raise InstallError("知识库目录不可用，请选择实际目录。")
        if sync not in {"local", "commit", "push"} or mode not in {"direct", "plugin"}:
            raise InstallError("同步方式或 Hook 方式无效。")
        default_home = Path.home() / ("Library/Application Support/AgentKnowledgeWorkflow" if sys.platform == "darwin"
                                      else ".local/state/agent-knowledge-workflow")
        if mode == "plugin" and self.home.resolve() != default_home.resolve():
            raise InstallError("插件模式请使用默认状态目录，或选择 direct 模式使用自定义目录。")
        if set(grants) != {"update", "commit", "push"} or any(type(v) is not bool for v in grants.values()):
            raise InstallError("三个授权选项均需明确选择 yes 或 no。")
        if grants["push"] and (not grants["commit"] or sync != "push") or grants["commit"] and sync == "local":
            raise InstallError("授权与所选同步方式不一致。请重新选择。")
        settings = {"auto_commit": grants["commit"], "auto_push": grants["push"]}
        if sync != "local":
            if not knowledge.is_dir() or git(knowledge, "rev-parse", "--show-toplevel") != str(knowledge):
                raise InstallError("自动提交需要知识库位于已有 Git 仓库根目录。安装器保留仓库状态。")
            branch = git(knowledge, "symbolic-ref", "--short", "HEAD")
            remotes = git(knowledge, "remote").splitlines()
            if remote is None and len(remotes) == 1:
                remote = remotes[0]
            if remote not in remotes:
                raise InstallError("请选择知识库的一个已有 Git 远端：" + ", ".join(remotes))
            urls = git(knowledge, "remote", "get-url", "--all", remote).splitlines()
            push_urls = git(knowledge, "remote", "get-url", "--push", "--all", remote).splitlines()
            if len(urls) != 1 or urls != push_urls:
                raise InstallError("远端读写 URL 不唯一或不一致。请先明确 Git 绑定。")
            url = urls[0]
            match = re.fullmatch(r"(?:git@github\.com:|https://github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\.git", url)
            local = Path(url).is_absolute() and Path(url).is_dir()
            if not match and not local:
                raise InstallError("现有引擎支持私有 GitHub 仓库及本地 fixture 远端。请选择受支持目标。")
            settings.update(remote=remote, branch=branch, remote_url=url,
                            repository=match[1] if match else "local-fixture")
            if local:
                settings["transport"] = "local"
            if not git(knowledge, "config", "user.name", required=False) or not git(knowledge, "config", "user.email", required=False):
                raise InstallError("Git 提交身份未配置。请在知识库配置身份后继续。")
            if grants["push"]:
                if match and not shutil.which("gh"):
                    raise InstallError("远端推送需要 GitHub CLI。安装并登录后继续：https://cli.github.com/")
                from .git_sync import _verify_binding, SyncError
                try:
                    _verify_binding(knowledge, settings)
                except SyncError:
                    raise InstallError("精确 Git 绑定或私有仓库检查未通过。请运行 doctor 并检查远端授权。") from None
        registry = self.registry()
        active = [m for m in registry["mappings"].values() if not m.get("archived")]
        for mapping in active:
            other = Path(mapping["project_root"])
            if other != project and (project.is_relative_to(other) or other.is_relative_to(project)):
                raise InstallError("项目范围有重叠，自动 Hook 无法选择映射。请选择不重叠的项目范围。")
        config_base = dict(project_root=str(project), knowledge_root=str(knowledge), enabled=grants["update"],
                           environment="local", git=settings, project_snapshot=False)
        if expires_at:
            try:
                expiry = dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if expiry.tzinfo is None or expiry <= dt.datetime.now(dt.timezone.utc):
                    raise ValueError()
            except ValueError:
                raise InstallError("期限需要带时区的未来 ISO-8601 时间。请重新选择期限。") from None
            config_base["expires_at"] = expires_at
        same = next((m for m in active if m["project_root"] == str(project)), None)
        restore_registration = False
        if same and same["hook_mode"] == mode and same["grants"] == grants and same["sync"] == sync:
            previous = self.config(same)
            restore_registration = bool(same.get("hooks_uninstalled") and
                                        all(previous.get(k) == v for k, v in config_base.items() if k != "enabled"))
            if not legacy_config and not restore_registration and all(previous.get(k) == v for k, v in config_base.items()):
                if mode == "direct":
                    doc = hooks_document(project / ".codex/hooks.json")
                    if any(doc.get("hooks", {}).get(event, []).count(group) != 1 for event, group in same["owned_hooks"].items()):
                        raise InstallError("现有 AKF Hook 缺失或重复，请先核对冲突。")
                result = {"state": "no_op", "mapping": same["id"], "preview_id": sha(encoded(previous)),
                          "next_step": "运行 akf doctor 查看状态。"}
                return result
        if restore_registration:
            identity = same["id"]
            config = {**previous, "enabled": grants["update"]}
            path = Path(same["config_path"])
        else:
            identity = sha(encoded({"config": config_base, "mode": mode, "registry": registry}))[:32]
            config = {**config_base, "knowledge_id": "akf-" + identity, "state_dir": str(self.home / "state" / identity)}
            path = self.home / "configs" / (identity + ".json")
        mapping = dict(id=identity, project_root=str(project), knowledge_root=str(knowledge),
                       config_path=str(path), hook_mode=mode, sync=sync, grants=grants,
                       paused=False, disabled=False, config_sha256=sha(encoded(config)))
        if restore_registration:
            for key in ("ownership_seed_sha256", "ownership_sources", "ownership_fact_count"):
                if key in same:
                    mapping[key] = same[key]
        changes = []
        ownership = {"fact_count": 0, "sources": []}
        hook_file = project / ".codex/hooks.json"
        doc = hooks_document(hook_file)
        groups = self.hook_group()
        legacy_source = None
        if legacy_config:
            old_path = Path(legacy_config).expanduser().resolve(strict=True)
            with old_path.open("rb") as stream:
                old_raw = stream.read(4_194_305)
            try:
                if len(old_raw) > 4_194_304:
                    raise ValueError()
                old_config = json.loads(old_raw)
            except (ValueError, UnicodeError):
                raise InstallError("所选旧配置无法解析，已保留原文件。") from None
            required = {"project_root", "knowledge_root", "state_dir", "knowledge_id"}
            if (not isinstance(old_config, dict) or not required <= old_config.keys()
                    or any(not isinstance(old_config[k], str) for k in required)):
                raise InstallError("所选旧配置无法识别，已保留原文件。")
            legacy_sha256 = sha(old_raw)
            old_project = Path(old_config["project_root"]).expanduser()
            if not old_project.is_absolute():
                old_project = old_path.parent / old_project
            if old_project.resolve() != project:
                raise InstallError("旧配置属于其他项目，已保留原 Hooks。")
            matched = 0
            for event, existing_groups in doc.get("hooks", {}).items():
                remaining = []
                for group in existing_groups:
                    kept = []
                    for handler in group["hooks"]:
                        try:
                            words = shlex.split(handler.get("command", ""))
                        except ValueError:
                            words = []
                        if len(words) > 1 and words[1] == "-B":
                            words.pop(1)
                        owned = (len(words) == 5 and re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(words[0]).name)
                                 and words[2] == "--config" and Path(words[3]).expanduser().resolve() == old_path
                                 and words[4] == "hook" and Path(words[1]).is_absolute()
                                 and Path(words[1]).name == "__main__.py" and Path(words[1]).parent.name == "akf"
                                 and event in EVENTS and handler.get("type") == "command")
                        if owned:
                            matched += 1
                        else:
                            kept.append(handler)
                    if kept or set(group) != {"hooks"}:
                        remaining.append({**group, "hooks": kept})
                doc["hooks"][event] = remaining
            if not matched:
                raise InstallError("未找到与所选旧配置精确匹配的直接 Hooks。请检查配置来源。")
            old_knowledge = Path(old_config["knowledge_root"]).expanduser()
            if not old_knowledge.is_absolute():
                old_knowledge = old_path.parent / old_knowledge
            legacy_source = {"id": "legacy-" + sha(encoded([str(old_path), legacy_sha256]))[:32],
                             "project_root": str(old_project.resolve()), "knowledge_root": str(old_knowledge.resolve()),
                             "config_path": str(old_path), "config_sha256": legacy_sha256}
        if not restore_registration:
            if Path(config["state_dir"]).exists():
                raise InstallError("目标状态目录已存在，已保留；请先检查恢复记录。")
            sources = dict(registry["mappings"])
            if legacy_source:
                sources[legacy_source["id"]] = legacy_source
            seed, ownership = self.ownership_seed(config, sources, same["id"] if same else None)
            if seed is not None:
                mapping.update(ownership_seed_sha256=sha(seed), ownership_sources=ownership["sources"],
                               ownership_fact_count=ownership["fact_count"])
                changes.append((Path(config["state_dir"]) / "state.sqlite3", seed, 0o600))
        if same and not restore_registration:
            same["archived"] = True
            previous = self.config(same)
            previous["enabled"] = False
            same["config_sha256"] = sha(encoded(previous))
            changes.append((Path(same["config_path"]), encoded(previous), 0o600))
        registry["mappings"][identity] = mapping
        # Remove only exact groups recorded by this installation, then add if needed.
        if same and same.get("owned_hooks") and not same.get("hooks_uninstalled"):
            for event, group in same["owned_hooks"].items():
                if group not in doc.get("hooks", {}).get(event, []):
                    raise InstallError("旧 AKF Hook 已改变，请先检查冲突。")
                doc["hooks"][event].remove(group)
        if has_akf_command(doc.get("hooks", {})):
            raise InstallError("发现未归属当前记录的 AKF Hook。请保留旧配置并确认迁移范围。")
        if mode == "direct":
            for event, group in groups.items():
                doc.setdefault("hooks", {}).setdefault(event, []).append(group)
            mapping["owned_hooks"] = groups
        if mode == "direct" or legacy_config or same and same.get("owned_hooks"):
            changes.append((hook_file, encoded(doc), stat.S_IMODE(hook_file.stat().st_mode) if hook_file.exists() else 0o600))
        # Inline hooks remain byte-for-byte untouched; Codex merges both representations.
        inline = project / ".codex/config.toml"
        for inline in (inline, self.codex / "config.toml"):
            if not inline.exists():
                continue
            try:
                inline_doc = tomllib.loads(inline.read_text())
            except (ValueError, UnicodeError):
                raise InstallError("config.toml 无法解析，已保留。请修正后重新设置。") from None
            if has_akf_command(inline_doc.get("hooks", {})):
                raise InstallError("发现旧 inline AKF Hook。请先明确迁移范围；原文件已保留。")
        global_hooks = self.codex / "hooks.json"
        if global_hooks.exists() and has_akf_command(hooks_document(global_hooks).get("hooks", {})):
            raise InstallError("发现已有用户级 AKF Hook。请先明确迁移范围；原文件已保留。")
        changes.extend([(path, encoded(config), 0o600), (self.registry_path, encoded(registry), 0o600)])
        preview_state = [{"path": str(p), "before": snapshot(p), "after": sha(data)} for p, data, _ in changes]
        if legacy_source:
            preview_state.append({"legacy_config": str(old_path), "config_sha256": legacy_sha256})
        preview_id = sha(encoded(preview_state))
        if not apply:
            return {"state": "preview", "preview_id": preview_id, "project_read": str(project),
                    "knowledge_write": str(knowledge), "state_directory": config["state_dir"],
                    "hook_source": str(hook_file) if mode == "direct" else "Codex 原生插件内置 Hooks",
                    "legacy_config": str(old_path) if legacy_config else None,
                    "grants": grants, "git_remote": settings.get("remote_url"), "branch": settings.get("branch"),
                    "changes": [str(p) for p, _, _ in changes], "mapping": identity, "ownership": ownership}
        if expected is not None and expected != preview_id:
            raise InstallError("预览后文件已变化。请重新运行 setup。")
        return {**self.transaction(changes, "setup", [knowledge]), "mapping": identity, "ownership": ownership,
                "next_step": "在 Codex 原生 /hooks 中审查并信任；执行普通任务后运行 akf doctor --verify。"}

    def control(self, identity, action):
        registry = self.registry()
        mapping = registry["mappings"][identity]
        config = self.config(mapping)
        if action == "enable":
            if not mapping["grants"]["update"]:
                raise InstallError("自动更新尚未授权。请重新运行 akf setup 单独确认。")
            mapping.update(paused=False, disabled=False)
            config["enabled"] = True
        else:
            mapping["paused" if action == "pause" else "disabled"] = True
            config["enabled"] = False
        mapping.pop("verified_package", None)
        mapping["config_sha256"] = sha(encoded(config))
        return self.transaction([(Path(mapping["config_path"]), encoded(config), 0o600),
                                 (self.registry_path, encoded(registry), 0o600)], action)

    def uninstall(self):
        registry = self.registry()
        if not registry.get("installed"):
            return {"state": "no_op"}
        changes = []
        for mapping in registry["mappings"].values():
            config = self.config(mapping)
            config["enabled"] = False
            mapping["config_sha256"] = sha(encoded(config))
            changes.append((Path(mapping["config_path"]), encoded(config), 0o600))
            mapping["disabled"] = True
            mapping.pop("verified_package", None)
            if mapping.get("archived") or not mapping.get("owned_hooks"):
                continue
            path = Path(mapping["project_root"]) / ".codex/hooks.json"
            if not path.exists():
                continue
            doc = hooks_document(path)
            for event, group in mapping["owned_hooks"].items():
                groups = doc.get("hooks", {}).get(event, [])
                if group in groups:
                    groups.remove(group)
                elif any("managed-hook" in h.get("command", "") for g in groups for h in g["hooks"]):
                    raise InstallError("AKF Hook 已被修改。请先核对冲突；当前文件已保留。")
            changes.append((path, encoded(doc), stat.S_IMODE(path.stat().st_mode)))
        if self.bin.exists():
            if sha(self.bin.read_bytes()) != registry["launcher_sha256"]:
                raise InstallError("启动入口已被修改，已保留。请先检查归属。")
            changes.append((self.bin, None, 0o755))
        pointer = self.code / "current.json"
        if pointer.exists() and sha(pointer.read_bytes()) != registry.get("current_sha256"):
            raise InstallError("版本入口已被修改，已保留。请先检查恢复记录。")
        registry["installed"] = False
        changes.extend([(self.code / "current.json", None, 0o600), (self.registry_path, encoded(registry), 0o600)])
        return {**self.transaction(changes, "uninstall"),
                "retained": "知识库、配置、pending jobs、状态、事务及版本恢复副本", 
                "next_step": "插件入口请在 Codex 插件界面卸载；使用安装包入口可回滚此事务。"}
