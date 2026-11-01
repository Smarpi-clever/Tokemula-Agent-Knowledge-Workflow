import copy
import datetime as dt
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

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
                         {"job": "job", "session": "session", "native_hook_run": "native-run", "outcome": "write"})
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
                         {"job": "job", "session": "session", "native_hook_run": "native-run", "outcome": "write"})
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


class NativeNoChangeTests(unittest.TestCase):
    def setUp(self):
        from akf.engine import Engine
        scratch = Path(__file__).resolve().parents[1] / ".local/tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="native-no-change-", dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.knowledge = self.root / "knowledge"
        self.project.mkdir()
        (self.knowledge / "topics").mkdir(parents=True)
        self.source = self.project / "settings.py"
        self.source.write_text("LIMIT = 4\n")
        self.note = self.knowledge / "topics/limits.md"
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"knowledge_id": "native-no-change", "enabled": True,
                              "project_root": str(self.project), "knowledge_root": str(self.knowledge),
                              "state_dir": str(self.root / "state"), "project_snapshot": False}))
        self.mapping = {"config_path": str(self.config)}
        self.engine = Engine(self.config)
        self.addCleanup(self.engine.db.close)
        self.job = self.new_job("ordinary-task")
        self.engine.submit(self.job["id"], {"status": "no_change", "updates": []})
        self.job = self.engine._get("job", self.job["id"])

    def new_job(self, turn):
        self.engine.start_turn("synthetic-native-session", turn, "a" * 64)
        self.engine.record_receipt("synthetic-native-session", turn, "tool-" + turn, "Read",
                                   {"file_path": "settings.py"}, {})
        return self.engine.prepare_maintenance("synthetic-native-session", turn, source="Stop")

    def candidate(self):
        from akf.engine import now
        return {"status": "completed", "updates": [{"topic": "topics/limits.md", "title": "Limits",
                "claim_id": "configured-limit", "claim_type": "file_fact", "content": "The limit is four.",
                "expected_sha256": sha(self.note.read_bytes()) if self.note.exists() else None,
                "observed_at": now(), "evidence": [{"kind": "file", "path": "settings.py",
                "sha256": sha(self.source.read_bytes()), "excerpt": "LIMIT = 4"}]}]}

    def duplicate_job(self):
        first = self.new_job("first-fact")
        self.assertEqual(self.engine.submit(first["id"], self.candidate())["state"], "local_saved")
        second = self.new_job("same-fact")
        self.assertEqual(self.engine.submit(second["id"], self.candidate())["state"], "no_change")
        self.job = self.engine._get("job", second["id"])

    def client(self, **fragment):
        client = Mock()
        client.request.return_value = {"data": [{"turnId": self.job.get("maintenance_turn_id") or self.job["turn_id"],
            "item": {"type": "hookPrompt", "fragments": [{"text": self.job["prompt_marker"] + "\nMaintain knowledge.",
                                                         "hookRunId": "synthetic-native-run", **fragment}]}}]}
        return client

    def verify(self, client=None):
        from datetime import datetime
        since = datetime.fromisoformat(self.job["created_at"]).timestamp()
        return verify_job(client or self.client(), self.mapping, since)

    def test_native_empty_no_change_verifies_without_knowledge_or_state_writes(self):
        before = (self.root / "state/state.sqlite3").read_bytes()
        self.assertEqual(self.verify(), {"job": self.job["id"], "session": "synthetic-native-session",
                         "native_hook_run": "synthetic-native-run", "outcome": "no_change"})
        self.assertFalse(self.note.exists())
        self.assertEqual((self.root / "state/state.sqlite3").read_bytes(), before)

    def test_same_fact_no_change_verifies_only_while_topic_and_source_match(self):
        self.duplicate_job()
        note_before = self.note.read_bytes()
        proof = self.verify()
        self.assertIsNotNone(proof)
        self.assertEqual(proof["outcome"], "no_change")
        self.assertEqual(self.note.read_bytes(), note_before)
        self.source.write_text("LIMIT = 8\n")
        self.assertIsNone(self.verify())
        self.source.write_text("LIMIT = 4\n")
        self.note.write_text("Human changed the note.\n")
        self.assertIsNone(self.verify())

    def test_no_change_rejects_pending_write_error_and_nonterminal_states(self):
        original = dict(self.job)
        cases = [("state", value) for value in ("candidate_pending", "applying", "failed", "cancelled", "rejected")]
        cases += [(key, value) for key, value in (
            ("pending_updates", [{"error": "Missing synthetic evidence"}]), ("plans", [{"topic": "topics/limits.md"}]),
            ("write_state", "local_saved"), ("write_history", [{"write_state": "local_saved"}]),
            ("write_id", "previous-write"), ("commit", "b" * 40), ("commit_parent", "c" * 40),
            ("stage_owned", True), ("push_attempted", True), ("remote_head", "d" * 40),
            ("undo_job", "undo"), ("error", "Unresolved synthetic error"))]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.engine._put("job", original["id"], {**original, key: value})
                client = self.client()
                self.assertIsNone(self.verify(client))
                client.request.assert_not_called()

    def test_no_change_still_needs_matching_native_nonce_type_run_and_turn(self):
        for fragment in ({"text": "different-marker\nMaintain knowledge."}, {"hookRunId": None}):
            with self.subTest(fragment=fragment):
                self.assertIsNone(self.verify(self.client(**fragment)))
        client = self.client()
        client.request.return_value["data"][0]["item"]["type"] = "userMessage"
        self.assertIsNone(self.verify(client))
        client = self.client()
        client.request.return_value["data"][0]["turnId"] = "unrelated-turn"
        self.assertIsNone(self.verify(client))
        self.assertIsNone(verify_job(self.client(), self.mapping, since=9999999999))

    def test_no_change_observed_digest_and_paths_remain_bound(self):
        self.job["source_files"] = [{"path": "settings.py", "sha256": sha(self.source.read_bytes())}]
        self.engine._put("job", self.job["id"], self.job)
        self.assertEqual(self.verify()["outcome"], "no_change")
        self.source.write_text("LIMIT = 8\n")
        self.assertIsNone(self.verify())
        outside = self.root / "outside.txt"
        outside.write_text("Synthetic outside evidence.\n")
        self.job["source_files"] = [{"path": "../outside.txt", "sha256": sha(outside.read_bytes())}]
        self.engine._put("job", self.job["id"], self.job)
        original_read = Path.read_bytes

        def inside_only(path):
            self.assertNotEqual(path.resolve(), outside.resolve())
            return original_read(path)

        with patch.object(Path, "read_bytes", autospec=True, side_effect=inside_only):
            self.assertIsNone(self.verify())


class NativePrefinalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-prefinal-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.knowledge = self.root / "knowledge"
        (self.knowledge / "topics").mkdir(parents=True)
        self.codex = self.root / "codex"
        self.rollout = self.codex / "sessions/2026/11/01/rollout-synthetic.jsonl"
        self.rollout.parent.mkdir(parents=True)
        environment = patch.dict("os.environ", {"CODEX_HOME": str(self.codex)})
        environment.start()
        self.addCleanup(environment.stop)
        config = self.root / "config.json"
        config.write_text(json.dumps({"state_dir": str(self.root), "project_root": str(self.project),
                                      "knowledge_root": str(self.knowledge)}))
        self.mapping = {"config_path": str(config)}
        self.marker = "[AKF-PREFINAL:" + "a" * 32 + "]"
        self.trigger_time = dt.datetime(2026, 11, 1, 5, tzinfo=dt.timezone.utc).timestamp()
        receipt = {"session_id": "native-session", "turn_id": "task-turn", "tool_use_id": "exec-trigger",
                   "tool_name": "Bash", "command_sha256": sha(b"pwd"),
                   "observed_at": dt.datetime.fromtimestamp(self.trigger_time, dt.timezone.utc).isoformat()}
        receipt["id"] = sha(b"native-session:task-turn:exec-trigger")[:24]
        self.job = {"id": "prefinal-job", "session_id": "native-session", "turn_id": "task-turn",
                    "state": "no_change", "created_at": "2026-11-01T05:01:00+00:00",
                    "preparation_source": "prefinal", "trigger_event": "PostToolUse", "trigger_marker": self.marker,
                    "trigger_tool_use_id": "exec-trigger", "trigger_receipt_id": receipt["id"], "receipts": [receipt]}
        self.database = self.root / "state.sqlite3"
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TABLE state (kind TEXT, value TEXT)")
            connection.execute("INSERT INTO state VALUES ('job', ?)", (json.dumps(self.job),))
        self.message = {"type": "response_item", "payload": {"type": "message", "role": "developer",
                        "content": [{"type": "input_text", "text": self.marker + "\nMaintain before final response."}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": "task-turn",
                            "create_time": self.trigger_time + 1, "content_item_kinds": ["hooks.additional_context"],
                            "content_item_metadata": [{"provenance": {"type": "harness", "hook": True}}]}}}
        self.thread = {"id": "native-session", "cwd": str(self.project), "path": str(self.rollout)}
        self.native_item = {"turnId": "task-turn", "item": {"type": "commandExecution", "id": "exec-trigger",
                            "command": "/bin/zsh -lc pwd", "status": "completed", "exitCode": 0}}
        self.save()

    def save(self):
        self.rollout.write_text(json.dumps(self.message) + "\n")
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE state SET value=? WHERE kind='job'", (json.dumps(self.job),))

    def client(self, pages=None):
        pages = iter(pages or [{"data": [self.native_item], "nextCursor": None}])
        client = Mock()

        def request(method, params):
            if method == "thread/read":
                self.assertEqual(params, {"threadId": "native-session", "includeTurns": False})
                return {"thread": self.thread}
            self.assertEqual(method, "thread/items/list")
            self.assertEqual(params["threadId"], "native-session")
            self.assertEqual(params["turnId"], "task-turn")
            return next(pages)

        client.request.side_effect = request
        return client

    def verify(self, client=None):
        return verify_job(client or self.client(), self.mapping, self.trigger_time - 1)

    def test_hidden_native_context_and_original_tool_verify_without_writes(self):
        before = self.database.read_bytes()
        self.assertEqual(self.verify(), {"job": "prefinal-job", "session": "native-session", "outcome": "no_change",
                         "native_hook_context": self.marker, "native_tool_use_id": "exec-trigger", "source": "prefinal"})
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(list((self.knowledge / "topics").iterdir()), [])
        (self.project / "src").mkdir()
        self.thread["cwd"] = str(self.project / "src")
        self.assertIsNotNone(self.verify())

    def test_context_provenance_role_and_content_index_are_bound(self):
        original = copy.deepcopy(self.message)
        for role in ("user", "assistant"):
            with self.subTest(role=role):
                self.message = copy.deepcopy(original)
                self.message["payload"]["role"] = role
                self.save()
                self.assertIsNone(self.verify())
        for change in ("no-provenance", "wrong-source", "hook-false", "wrong-kind", "mixed-content", "non-text"):
            with self.subTest(change=change):
                self.message = copy.deepcopy(original)
                payload = self.message["payload"]
                metadata = payload["internal_chat_message_metadata_passthrough"]
                if change == "no-provenance":
                    metadata["content_item_metadata"] = [{}]
                elif change == "wrong-source":
                    metadata["content_item_metadata"][0]["provenance"]["type"] = "user"
                elif change == "hook-false":
                    metadata["content_item_metadata"][0]["provenance"]["hook"] = False
                elif change == "wrong-kind":
                    metadata["content_item_kinds"] = ["text"]
                elif change == "non-text":
                    payload["content"][0]["text"] = None
                else:
                    payload["content"].append({"type": "input_text", "text": "Unrelated hook context."})
                    metadata["content_item_kinds"] = ["text", "hooks.additional_context"]
                    metadata["content_item_metadata"] = [{}, {"provenance": {"type": "harness", "hook": True}}]
                self.save()
                self.assertIsNone(self.verify())

    def test_context_marker_turn_session_cwd_path_and_time_are_bound(self):
        original = copy.deepcopy(self.message)
        for change in ("marker", "turn", "old", "future"):
            with self.subTest(change=change):
                self.message = copy.deepcopy(original)
                payload = self.message["payload"]
                metadata = payload["internal_chat_message_metadata_passthrough"]
                if change == "marker":
                    payload["content"][0]["text"] = "Another marker\n" + self.marker
                elif change == "turn":
                    metadata["turn_id"] = "other-turn"
                else:
                    metadata["create_time"] = self.trigger_time - 10 if change == "old" else self.trigger_time + 120
                self.save()
                self.assertIsNone(self.verify())
        self.message = original
        self.save()
        original_thread = dict(self.thread)
        for field, value in (("id", "another-session"), ("cwd", str(self.knowledge)), ("path", None),
                             ("path", str(self.mapping["config_path"]))):
            with self.subTest(field=field, value=value):
                self.thread = {**original_thread, field: value}
                self.assertIsNone(self.verify())

    def test_trigger_native_id_command_digest_and_receipt_identity_are_bound(self):
        original_item = copy.deepcopy(self.native_item)
        for field, value in (("id", "another-tool"), ("type", "agentMessage"), ("command", "/bin/zsh -lc false")):
            with self.subTest(field=field):
                self.native_item = copy.deepcopy(original_item)
                self.native_item["item"][field] = value
                self.assertIsNone(self.verify())
        self.native_item = original_item
        self.native_item["turnId"] = "another-turn"
        self.assertIsNone(self.verify())
        self.native_item = copy.deepcopy(original_item)
        self.native_item["turnId"] = "task-turn"
        self.job["receipts"][0]["id"] = "forged"
        self.job["trigger_receipt_id"] = "forged"
        self.save()
        self.assertIsNone(self.verify())

    def test_prefinal_native_item_pagination_and_bounded_missing_context(self):
        client = self.client([{"data": [], "nextCursor": "page-two"}, {"data": [self.native_item], "nextCursor": None}])
        self.assertIsNotNone(self.verify(client))
        self.assertEqual(client.request.call_args_list[-1].args[1]["cursor"], "page-two")
        self.rollout.write_bytes(self.rollout.read_bytes() + b"\n" * 4_194_305)
        self.assertIsNone(self.verify())

    def test_write_still_requires_matching_knowledge_digest(self):
        note = self.knowledge / "topics/synthetic.md"
        note.write_text("Verified synthetic knowledge.\n")
        self.job.update(state="applied", write_state="local_saved",
                        plans=[{"topic": "topics/synthetic.md", "after_sha256": sha(note.read_bytes())}])
        self.save()
        self.assertEqual(self.verify()["outcome"], "write")
        note.write_text("Changed outside the maintenance job.\n")
        self.assertIsNone(self.verify())


if __name__ == "__main__":
    unittest.main()
