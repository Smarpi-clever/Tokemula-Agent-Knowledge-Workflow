"""Read-only Codex protocol queries. Never start a daemon, turn or trust action."""
import json
import os
import selectors
import shutil
import subprocess
import time

from . import __version__
from .install import InstallError


class NativeClient:
    def __enter__(self):
        if not shutil.which("codex"):
            raise InstallError("Codex CLI 未找到。请安装后重新运行 doctor。")
        self.process = subprocess.Popen(
            ["codex", "--no-daemon", "app-server", "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b""
        self.counter = 0
        try:
            self.request("initialize", {"clientInfo": {"name": "akf-installer", "version": __version__}})
            self.send({"method": "initialized"})
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *args):
        self.selector.close()
        self.process.stdin.close()
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)
        self.process.stdout.close()

    def send(self, value):
        try:
            self.process.stdin.write(json.dumps(value).encode() + b"\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise InstallError("Codex 查询进程已关闭，请重新运行 doctor。") from None

    def request(self, method, params):
        self.counter += 1
        identity = self.counter
        self.send({"id": identity, "method": method, "params": params})
        deadline = time.monotonic() + 5
        size = 0
        while True:
            if b"\n" not in self.buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self.selector.select(remaining):
                    raise InstallError("Codex 状态查询超时，请重新运行 doctor。")
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise InstallError("Codex 查询进程已关闭，请重新运行 doctor。")
                self.buffer += chunk
                size += len(chunk)
                if size > 4_194_304 or len(self.buffer) > 4_194_304:
                    raise InstallError("Codex 查询结果过大。请在原生界面确认。")
                continue
            line, self.buffer = self.buffer.split(b"\n", 1)
            try:
                message = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                raise InstallError("Codex 查询返回了无效 JSON。") from None
            if message.get("id") == identity:
                if "error" in message:
                    error = message["error"]
                    code = error.get("code") if isinstance(error, dict) else None
                    detail = ", code " + str(code) if isinstance(code, int) and not isinstance(code, bool) else ""
                    raise InstallError("当前客户端尚未提供此查询（" + method + detail + "）。请在原生界面确认。")
                return message["result"]

def hook_status(client, manager, mapping):
    result = client.request("hooks/list", {"cwds": [mapping["project_root"]]})
    rows = [row for row in result.get("data", []) if row.get("cwd") == mapping["project_root"]]
    if len(rows) != 1 or rows[0].get("errors"):
        return {"trusted": False, "active": False, "state": "待审查", "hashes": []}
    from pathlib import Path
    mode = mapping["hook_mode"]
    if mode == "direct":
        source = Path(mapping["project_root"]) / ".codex/hooks.json"
        command = manager.hook_group()["Stop"]["hooks"][0]["command"]
        hooks = [h for h in rows[0]["hooks"] if h.get("sourcePath") == str(source)
                 and h.get("command") == command]
    else:
        hooks = [h for h in rows[0]["hooks"] if (h.get("pluginId") or "").split("@")[0] == "agent-knowledge-workflow"
                 and h.get("source") == "plugin"]
    events = {"sessionStart", "userPromptSubmit", "postToolUse", "stop", "interrupt"}
    complete = len(hooks) == 5 and {h["eventName"] for h in hooks} == events
    trusted = complete and all(h.get("trustStatus") == "trusted" for h in hooks)
    active = complete and all(h.get("enabled") is True for h in hooks)
    return {"trusted": trusted, "active": active,
            "state": "已信任" if trusted else "待审查", "hashes": sorted(h["currentHash"] for h in hooks)}
