"""Read-only Codex protocol queries. Never start a daemon, turn or trust action."""
import json
import os
import base64
import hashlib
import socket
import struct
import shutil
import subprocess
import time

from .install import InstallError


class NativeClient:
    def __enter__(self):
        if not shutil.which("codex"):
            raise InstallError("Codex CLI 未找到。请安装后重新运行 doctor。")
        try:
            info = subprocess.run(["codex", "app-server", "daemon", "version"], capture_output=True, text=True, timeout=5)
        except subprocess.TimeoutExpired:
            raise InstallError("Codex 状态查询超时，请在原生界面确认。") from None
        meta = json.loads(info.stdout) if info.returncode == 0 else {}
        if meta.get("status") != "running" or not meta.get("socketPath"):
            raise InstallError("打开 Codex 客户端后重新运行 doctor。")
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(5)
        self.buffer = b""
        self.counter = 0
        try:
            self.socket.connect(meta["socketPath"])
            key = base64.b64encode(os.urandom(16)).decode()
            self.socket.sendall(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                                 "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: " + key + "\r\n\r\n").encode())
            while b"\r\n\r\n" not in self.buffer:
                chunk = self.socket.recv(4096)
                if not chunk:
                    raise InstallError("Codex 本地连接已关闭，请重新运行 doctor。")
                self.buffer += chunk
                if len(self.buffer) > 16384:
                    raise InstallError("Codex 本地连接握手无效。")
            headers, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
            fields = {line.split(b":", 1)[0].lower(): line.split(b":", 1)[1].strip()
                      for line in headers.split(b"\r\n")[1:] if b":" in line}
            if not headers.startswith(b"HTTP/1.1 101 ") or fields.get(b"sec-websocket-accept") != accept:
                raise InstallError("当前 Codex 本地连接不可用。请在原生界面确认。")
            self.request("initialize", {"clientInfo": {"name": "akf-installer", "version": "0.2.0"}})
            self.send({"method": "initialized"})
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *args):
        self.socket.close()

    def send(self, value):
        self.frame(1, json.dumps(value).encode())

    def frame(self, opcode, data):
        length = len(data)
        header = bytes([0x80 | opcode, 0x80 | length]) if length < 126 else bytes([0x80 | opcode, 0xFE]) + struct.pack("!H", length)
        mask = os.urandom(4)
        self.socket.sendall(header + mask + bytes(byte ^ mask[i % 4] for i, byte in enumerate(data)))

    def read(self, length):
        while len(self.buffer) < length:
            chunk = self.socket.recv(min(65536, length - len(self.buffer)))
            if not chunk:
                raise InstallError("Codex 本地连接已关闭，请重新运行 doctor。")
            self.buffer += chunk
        result, self.buffer = self.buffer[:length], self.buffer[length:]
        return result

    def request(self, method, params):
        self.counter += 1
        identity = self.counter
        self.send({"id": identity, "method": method, "params": params})
        deadline = time.monotonic() + 5
        size, parts = 0, []
        while time.monotonic() < deadline:
            self.socket.settimeout(max(.01, deadline - time.monotonic()))
            first, second = self.read(2)
            opcode = first & 15
            if first & 0x70 or second & 0x80:
                raise InstallError("Codex 本地连接帧无效。")
            length = second & 127
            if length == 126:
                length = struct.unpack("!H", self.read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self.read(8))[0]
            size += length
            if size > 4_194_304:
                raise InstallError("Codex 查询结果过大。请在原生界面确认。")
            data = self.read(length)
            if opcode == 8:
                raise InstallError("Codex 本地连接已关闭。")
            if opcode in {9, 10}:
                if opcode == 9:
                    self.frame(10, data)
                continue
            if opcode not in {0, 1}:
                raise InstallError("Codex 查询返回了未知帧类型。")
            parts.append(data)
            if not first & 0x80:
                continue
            message = json.loads(b"".join(parts))
            parts = []
            if message.get("id") == identity:
                if "error" in message:
                    raise InstallError("当前客户端尚未提供此查询。请在原生 /hooks 中确认。")
                return message["result"]
        raise InstallError("无法连接现有 Codex 客户端。打开客户端后重新运行 doctor。")


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
