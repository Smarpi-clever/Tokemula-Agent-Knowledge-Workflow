import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import Mock

from akf.native import NativeClient, hook_status
from akf.install import InstallError, Manager, EVENTS, sha
from akf.setup_cli import verify_job


def frame(data, opcode=1, final=True):
    header = bytes([(128 if final else 0) | opcode])
    length = len(data)
    return header + (bytes([length]) if length < 126 else bytes([126]) + struct.pack("!H", length)) + data


class FakeSocket:
    def __init__(self, incoming):
        self.incoming = incoming
        self.sent = []

    def recv(self, length):
        value, self.incoming = self.incoming[:length], self.incoming[length:]
        return value

    def sendall(self, data):
        self.sent.append(data)

    def settimeout(self, value):
        pass


class NativeReadTests(unittest.TestCase):
    def client(self, data):
        client = NativeClient()
        client.socket = FakeSocket(data)
        client.counter = 0
        client.buffer = b""
        return client

    def test_masked_request_fragmented_response_ping_and_notification(self):
        response = json.dumps({"id": 1, "result": {"data": []}}).encode()
        incoming = (frame(json.dumps({"method": "notification"}).encode()) + frame(b"ping", 9)
                    + frame(response[:10], final=False) + frame(response[10:], opcode=0))
        client = self.client(incoming)
        self.assertEqual(client.request("hooks/list", {"cwds": ["/synthetic"]}), {"data": []})
        sent = client.socket.sent[0]
        self.assertTrue(sent[1] & 128)
        length = sent[1] & 127
        mask = sent[2:6]
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(sent[6:6+length]))
        self.assertEqual(json.loads(data), {"id": 1, "method": "hooks/list", "params": {"cwds": ["/synthetic"]}})
        self.assertEqual(client.socket.sent[1][0] & 15, 10)

    def test_closed_connection_and_large_frame_fail_with_actionable_error(self):
        for data in (b"", frame(b"", 8), bytes([129, 127]) + struct.pack("!Q", 4_194_305)):
            with self.assertRaises(InstallError):
                self.client(data).request("hooks/list", {})

    def test_native_trust_requires_five_exact_handlers_enabled(self):
        manager = Manager()
        mapping = {"project_root": "/synthetic", "hook_mode": "direct"}
        command = manager.hook_group()["Stop"]["hooks"][0]["command"]
        hooks = [{"eventName": name[0].lower() + name[1:], "command": command,
                  "sourcePath": "/synthetic/.codex/hooks.json", "currentHash": name,
                  "trustStatus": "trusted", "enabled": True} for name in EVENTS]
        class Client:
            def request(self, *args):
                return {"data": [{"cwd": "/synthetic", "errors": [], "hooks": hooks}]}
        client = Client()
        self.assertTrue(hook_status(client, manager, mapping)["trusted"])
        hooks[0]["trustStatus"] = "modified"
        self.assertFalse(hook_status(client, manager, mapping)["trusted"])
        hooks[0]["trustStatus"] = "trusted"
        hooks[0]["enabled"] = False
        self.assertFalse(hook_status(client, manager, mapping)["active"])
        hooks.append(hooks[0])
        self.assertFalse(hook_status(client, manager, mapping)["trusted"])


class NativeJobPaginationTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[1] / ".local/tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="native-pages-", dir=scratch)
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        note = root / "topic.md"
        note.write_text("Verified synthetic fact.\n")
        config = root / "config.json"
        config.write_text(json.dumps({"state_dir": str(root), "knowledge_root": str(root)}))
        self.mapping = {"config_path": str(config)}
        self.job = {"id": "job", "session_id": "session", "turn_id": "task-turn", "maintenance_turn_id": "maintenance-turn",
                    "created_at": "2026-10-31T00:00:00+00:00", "write_state": "local_saved",
                    "prompt_marker": "unique-marker", "plans": [{"topic": note.name, "after_sha256": sha(note.read_bytes())}]}
        self.database = root / "state.sqlite3"
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TABLE state (kind TEXT, value TEXT)")
            connection.execute("INSERT INTO state VALUES ('job', ?)", (json.dumps(self.job),))
        self.first_page = {"data": [{"item": {"type": "commandExecution"}} for _ in range(20)],
                           "nextCursor": "opaque-next-page"}

    def test_verified_native_prompt_on_second_page(self):
        second = {"data": [{"item": {"type": "hookPrompt", "fragments": [
            {"text": "unique-marker\nMaintain knowledge.", "hookRunId": "native-run"}]}}], "nextCursor": None}
        client = Mock()
        client.request.side_effect = [self.first_page, second]
        self.assertEqual(verify_job(client, self.mapping),
                         {"job": "job", "session": "session", "native_hook_run": "native-run"})
        expected = {"threadId": "session", "turnId": "maintenance-turn", "limit": 20, "sortDirection": "desc"}
        self.assertEqual(client.request.call_args_list[0].args, ("thread/items/list", expected))
        self.assertEqual(client.request.call_args_list[1].args,
                         ("thread/items/list", {**expected, "cursor": "opaque-next-page"}))

    def test_exhausted_pages_without_matching_native_prompt_stay_unverified(self):
        second = {"data": [{"item": {"type": "hookPrompt", "fragments": [
            {"text": "different-marker\nMaintain knowledge.", "hookRunId": "unrelated-run"},
            {"text": "unique-marker\nMissing native run."}]}}], "nextCursor": None}
        client = Mock()
        client.request.side_effect = [self.first_page, second]
        self.assertIsNone(verify_job(client, self.mapping))
        self.assertEqual(client.request.call_count, 2)

    def test_same_turn_maintenance_verifies_native_prompt(self):
        self.job["maintenance_turn_id"] = None
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE state SET value=? WHERE kind='job'", (json.dumps(self.job),))
        client = Mock()
        client.request.return_value = {"data": [{"item": {"type": "hookPrompt", "fragments": [
            {"text": "unique-marker\nMaintain knowledge.", "hookRunId": "native-run"}]}}]}
        self.assertEqual(verify_job(client, self.mapping),
                         {"job": "job", "session": "session", "native_hook_run": "native-run"})
        client.request.assert_called_once_with("thread/items/list", {
            "threadId": "session", "turnId": "task-turn", "limit": 20, "sortDirection": "desc"})

    def test_same_turn_without_native_prompt_stays_unverified(self):
        self.job["maintenance_turn_id"] = None
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE state SET value=? WHERE kind='job'", (json.dumps(self.job),))
        client = Mock()
        client.request.return_value = {"data": [{"item": {"type": "agentMessage", "text": "unique-marker\nMaintain knowledge."}}]}
        self.assertIsNone(verify_job(client, self.mapping))
        client.request.assert_called_once_with("thread/items/list", {
            "threadId": "session", "turnId": "task-turn", "limit": 20, "sortDirection": "desc"})


if __name__ == "__main__":
    unittest.main()
