"""Task-evidence collection uses isolated fixtures and never exercises native trust."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from akf.engine import Engine, WorkflowError, binding_fingerprint, digest, now
from akf.hooks import handle_event
from akf.install import Manager, encoded


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".local/tmp"
SCRATCH.mkdir(parents=True, exist_ok=True)


class ReadScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="read-scope-", dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root / "project"
        self.knowledge = self.root / "knowledge"
        self.project.mkdir()
        (self.knowledge / "topics").mkdir(parents=True)
        self.source = self.project / "settings.py"
        self.source.write_text("LIMIT = 4\n")
        self.decoy = self.project / "unrelated-private.txt"
        self.decoy.write_text("Unrelated synthetic material.\n")
        self.config = self.root / "config.json"
        self.settings = {"knowledge_id": "read-scope", "project_root": str(self.project),
                         "knowledge_root": str(self.knowledge), "state_dir": str(self.root / "state"),
                         "enabled": True, "environment": "synthetic", "project_snapshot": False}
        self.config.write_text(json.dumps(self.settings))
        self.engine = Engine(self.config)
        self.addCleanup(self.engine.db.close)

    def event(self, name, turn="task", **fields):
        return {"hook_event_name": name, "cwd": str(self.project), "session_id": "scope-session",
                "turn_id": turn, **fields}

    def observed_job(self, turn="task", prompt=True):
        if prompt:
            handle_event(self.event("UserPromptSubmit", turn, prompt="Inspect the configured limit."), self.engine)
        hint = handle_event(self.event("PostToolUse", turn, tool_use_id="read-" + turn, tool_name="Bash",
                                      tool_input={"cmd": "cat settings.py"}, tool_response={"exit_code": 0}), self.engine)
        self.assertEqual(hint["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        job = self.engine.prepare_maintenance("scope-session", turn)
        self.assertEqual(job["trigger_marker"], hint["hookSpecificOutput"]["additionalContext"].splitlines()[0])
        self.assertEqual(job["preparation_source"], "prefinal")
        self.assertEqual(handle_event(self.event("Stop", turn), self.engine), {})
        return job

    def candidate(self):
        return {"status": "completed", "updates": [{"topic": "topics/limits.md", "title": "Limits",
                "claim_id": "configured-limit", "claim_type": "file_fact", "content": "The configured limit is four.",
                "expected_sha256": None, "observed_at": now(), "evidence": [{"kind": "file", "path": "settings.py",
                "sha256": digest(self.source.read_bytes()), "excerpt": "LIMIT = 4"}]}]}

    def test_prompt_receipt_stop_do_not_walk_or_read_project_files(self):
        original_read = Path.read_bytes
        project_reads = []

        def scoped_read(path):
            if path.is_relative_to(self.project):
                project_reads.append(path)
                raise AssertionError("Lifecycle collection must not read project content")
            return original_read(path)

        with patch("akf.engine.os.walk", side_effect=AssertionError("No project walk")), \
                patch.object(Path, "read_bytes", autospec=True, side_effect=scoped_read):
            job = self.observed_job()
            fallback = self.observed_job("missing-prompt", prompt=False)
        self.assertEqual(project_reads, [])
        for value in (job, fallback):
            self.assertEqual(value["source_files"], [])
            self.assertTrue(value["partial_evidence"])
            self.assertEqual(len(value["receipts"]), 1)
        self.assertEqual(fallback["origin"], "post_tool_observation")

    def test_only_submitted_source_is_read_and_receipts_remain_bound(self):
        job = self.observed_job()
        candidate = self.candidate()
        original_read = Path.read_bytes
        project_reads = []

        def scoped_read(path):
            if path.is_relative_to(self.project):
                self.assertEqual(path, self.source)
                project_reads.append(path)
            return original_read(path)

        with patch("akf.engine.os.walk", side_effect=AssertionError("No project walk")), \
                patch.object(Path, "read_bytes", autospec=True, side_effect=scoped_read):
            result = self.engine.submit(job["id"], candidate)
        self.assertEqual(result["state"], "local_saved")
        self.assertEqual(project_reads, [self.source])
        note = self.knowledge / "topics/limits.md"
        before = note.read_bytes()
        self.assertEqual(self.engine.submit(job["id"], candidate)["state"], "local_saved")
        self.assertEqual(note.read_bytes(), before)
        self.assertEqual(len(self.engine._all("receipt")), 1)

    def test_changed_source_stays_pending_and_escaping_path_is_rejected(self):
        job = self.observed_job()
        candidate = self.candidate()
        self.source.write_text("LIMIT = 9\n")
        self.assertEqual(self.engine.submit(job["id"], candidate)["state"], "pending_evidence")
        self.assertFalse((self.knowledge / "topics/limits.md").exists())
        candidate["updates"][0]["evidence"][0]["path"] = "../outside.py"
        rejected = self.engine.submit(job["id"], candidate)
        self.assertEqual(rejected["state"], "rejected")
        self.assertEqual(rejected["error"], "Hidden or escaping paths are not allowed")
        self.assertFalse((self.knowledge / "topics/limits.md").exists())

    def test_no_receipt_no_fact_and_cancelled_turn_never_write(self):
        handle_event(self.event("UserPromptSubmit", prompt="No task evidence."), self.engine)
        self.source.write_text("LIMIT = 8\n")
        self.assertEqual(handle_event(self.event("Stop"), self.engine), {})
        self.assertEqual(self.engine._all("job"), [])
        empty_job = self.observed_job("no-fact")
        self.assertEqual(self.engine.submit(empty_job["id"], {"status": "no_change", "updates": []})["state"], "no_change")
        handle_event(self.event("UserPromptSubmit", "cancelled", prompt="Inspect the limit."), self.engine)
        handle_event(self.event("Interrupt", "cancelled"), self.engine)
        with patch("akf.engine.os.walk", side_effect=AssertionError("No project walk")):
            handle_event(self.event("PostToolUse", "cancelled", tool_use_id="cancelled-tool", tool_name="Bash",
                                   tool_input={}, tool_response={"exit_code": 0}), self.engine)
            self.assertEqual(handle_event(self.event("Stop", "cancelled"), self.engine), {})
        self.assertEqual(len(self.engine._all("job")), 1)
        self.assertEqual(len(self.engine._all("receipt")), 1)
        self.assertEqual(list((self.knowledge / "topics").iterdir()), [])

    def test_fixed_commit_evidence_still_validates_without_project_scan(self):
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_TERMINAL_PROMPT": "0"}

        def git(*args):
            return subprocess.run(["git", "-C", str(self.project), *args], env=env,
                                  check=True, capture_output=True, text=True).stdout.strip()

        git("init", "--quiet")
        git("add", "settings.py")
        git("-c", "user.name=Synthetic", "-c", "user.email=fixture@example.invalid",
            "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "Synthetic source")
        commit = git("rev-parse", "HEAD")
        job = self.observed_job()
        candidate = self.candidate()
        candidate["updates"][0]["evidence"][0].update(kind="commit", commit=commit)
        self.source.write_text("LIMIT = 99\n")
        with patch("akf.engine.os.walk", side_effect=AssertionError("No project walk")):
            self.assertEqual(self.engine.submit(job["id"], candidate)["state"], "local_saved")

    def test_old_binding_and_database_remain_compatible(self):
        legacy = {key: value for key, value in self.settings.items() if key != "project_snapshot"}
        legacy["state_dir"] = str(self.root / "legacy-state")
        original_binding = {key: legacy.get(key) for key in ("knowledge_id", "environment", "expires_at", "git")}
        original_binding.update(project_root=str(self.project), knowledge_root=str(self.knowledge),
                                state_dir=legacy["state_dir"])
        expected = digest(json.dumps(original_binding, sort_keys=True).encode())
        self.assertEqual(binding_fingerprint(legacy, self.project, self.knowledge, Path(legacy["state_dir"])), expected)
        legacy_path = self.root / "legacy.json"
        legacy_path.write_text(json.dumps(legacy))
        engine = Engine(legacy_path)
        self.assertIn("settings.py", engine._snapshot())
        engine.db.close()
        db = Path(legacy["state_dir"]) / "state.sqlite3"
        before = db.read_bytes()
        reopened = Engine(legacy_path)
        reopened.db.close()
        self.assertEqual(db.read_bytes(), before)
        legacy["project_snapshot"] = False
        self.assertNotEqual(binding_fingerprint(legacy, self.project, self.knowledge, Path(legacy["state_dir"])), expected)

    def test_invalid_snapshot_switch_is_rejected_at_config_boundary(self):
        self.settings["project_snapshot"] = "false"
        self.config.write_text(json.dumps(self.settings))
        with self.assertRaisesRegex(WorkflowError, "Project snapshot must be a boolean"):
            Engine(self.config)

    def test_new_setup_records_task_evidence_collection_without_json_editing(self):
        env = {"AKF_HOME": str(self.root / "support"), "AKF_INSTALL_ROOT": str(self.root / "tool"),
               "AKF_BIN_DIR": str(self.root / "bin"), "CODEX_HOME": str(self.root / "codex")}
        with patch.dict(os.environ, env):
            manager = Manager()
            manager.home.mkdir()
            manager.registry_path.write_bytes(encoded({"schema": 1, "mappings": {}, "installed": True}))
            with manager.lock():
                result = manager.setup(self.project, self.knowledge, "local", "direct",
                                       {"update": False, "commit": False, "push": False})
            mapping = manager.registry()["mappings"][result["mapping"]]
            config = manager.config(mapping)
            self.assertIs(config["project_snapshot"], False)
            self.assertIs(config["enabled"], False)
            self.assertFalse(config["git"]["auto_commit"])
            self.assertFalse(config["git"]["auto_push"])


if __name__ == "__main__":
    unittest.main()
