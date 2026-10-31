import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from akf import git_sync
from akf.engine import Engine
import test_engine
from test_engine import Fixture, TMP
from test_git_sync import git


class PartialSyncTests(Fixture):
    def setUp(self):
        parent = tempfile.TemporaryDirectory(prefix="partial-sync-", dir=TMP)
        self.addCleanup(parent.cleanup)
        with patch.object(test_engine, "TMP", Path(parent.name)):
            super().setUp()
        environment = patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        self.engine.db.close()
        (self.root / "state" / "state.sqlite3").unlink()
        self.note = self.knowledge / "topics" / "limits.md"
        self.human = "# Request limits\n\nHuman introduction stays.\n"
        self.note.write_text(self.human)
        git(self.knowledge, "init", "-b", "main")
        git(self.knowledge, "config", "user.name", "Synthetic Fixture")
        git(self.knowledge, "config", "user.email", "fixture@example.test")
        git(self.knowledge, "add", "--", "topics/limits.md")
        git(self.knowledge, "commit", "-m", "human baseline")
        self.remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(self.remote)], capture_output=True, check=True)
        git(self.knowledge, "remote", "add", "origin", str(self.remote))
        git(self.knowledge, "push", "-u", "origin", "main")
        self.settings["git"] = {"auto_commit": True, "auto_push": True, "remote": "origin",
            "branch": "main", "remote_url": str(self.remote), "transport": "local",
            "repository": "local-fixture"}
        self.config.write_text(json.dumps(self.settings))
        self.engine = Engine(self.config)
        self.addCleanup(self.engine.db.close)
        self.baseline = git(self.knowledge, "rev-parse", "HEAD")

    def mixed(self, job, *, deployment=False):
        candidate = self.candidate(job)
        source = copy.deepcopy(candidate["updates"][0])
        source.update(claim_id="limit-source", content="The limit is defined in settings.py.")
        pending = copy.deepcopy(source)
        pending.update(claim_id="limit-tests", claim_type="test_result",
                       content="The focused limit test passed.", evidence=[])
        candidate["status"] = "partial"
        candidate["updates"].extend([source, pending])
        if deployment:
            deploy = copy.deepcopy(pending)
            deploy.update(claim_id="limit-deployment", claim_type="deployment",
                          content="The updated limit was deployed.")
            candidate["updates"].append(deploy)
        return candidate

    def job(self):
        self.engine.start_turn("session", "turn", "a" * 64)
        (self.project / "settings.py").write_text("LIMIT = 8\n")
        (self.project / "test_settings.py").write_text(
            "import unittest\nfrom settings import LIMIT\n"
            "class LimitTests(unittest.TestCase):\n"
            "    def test_limit(self):\n        self.assertEqual(LIMIT, 8)\n")
        process = subprocess.run([sys.executable, "-B", "-m", "unittest"], cwd=self.project,
                                 capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("Ran 1 test", process.stderr)
        self.receipt = self.engine.record_receipt("session", "turn", "limit-test",
            "Bash", {"cmd": "python3 -B -m unittest"}, {"exit_code": process.returncode})
        return self.engine.prepare_maintenance("session", "turn")

    def complete_test(self, job, pending):
        update = copy.deepcopy(pending)
        update["evidence"] = [{"kind": "receipt", "receipt_id": self.receipt["receipt_id"], "expected_exit": 0}]
        return {"status": "partial", "updates": [update]}

    def assert_synced(self, result, commits):
        self.assertEqual(result["state"], "remote_confirmed", result)
        head = git(self.knowledge, "rev-parse", "HEAD")
        self.assertEqual(result["commit"], head)
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/main"), head)
        remote_bytes = subprocess.check_output(["git", "--git-dir", str(self.remote),
                                               "show", head + ":topics/limits.md"])
        self.assertEqual(remote_bytes, self.note.read_bytes())
        self.assertEqual(git(self.knowledge, "rev-list", "--count", "HEAD"), str(commits))
        self.assertEqual(git(self.remote, "rev-list", "--count", "refs/heads/main"), str(commits))
        self.assertEqual(git(self.knowledge, "status", "--porcelain"), "")

    def test_same_topic_verified_facts_sync_while_test_and_deployment_stay_pending(self):
        job = self.job()
        candidate = self.mixed(job, deployment=True)
        result = self.engine.submit(job["id"], candidate)
        self.assert_synced(result, 2)
        self.assertEqual(result["write_state"], "remote_confirmed")
        self.assertEqual(result["pending_count"], 2)
        self.assertEqual([p["update"] for p in result["pending_updates"]], candidate["updates"][2:])
        self.assertTrue(all(p["error"] for p in result["pending_updates"]))
        text = self.note.read_text()
        self.assertTrue(text.startswith(self.human))
        for update in candidate["updates"][:2]:
            self.assertIn(update["content"], text)
            self.assertEqual(text.count("<!-- akf:" + update["claim_id"] + " -->"), 1)
        for update in candidate["updates"][2:]:
            self.assertNotIn(update["content"], text)
        self.assertEqual(self.engine.status(job["id"])["pending_count"], 2)
        self.assertEqual(self.engine.status()["jobs"][-1]["pending_count"], 2)
        self.assertEqual(self.engine.context()["pending_jobs"][-1]["pending_count"], 2)

    def test_completing_pending_creates_one_new_batch_and_duplicate_calls_do_not_commit(self):
        job = self.job()
        candidate = self.mixed(job)
        first = self.engine.submit(job["id"], candidate)
        self.assert_synced(first, 2)
        completed = self.complete_test(job, candidate["updates"][2])
        second = self.engine.submit(job["id"], completed)
        self.assert_synced(second, 3)
        self.assertNotEqual(first["commit"], second["commit"])
        self.assertEqual(second["pending_count"], 0)
        self.assertEqual(git(self.knowledge, "rev-parse", second["commit"] + "^"), first["commit"])
        status = self.engine.status(job["id"])
        self.assertEqual(status["write_id"], job["id"] + ":2")
        self.assertEqual(len(status["write_history"]), 1)
        for commit, marker in ((first["commit"], job["id"]), (second["commit"], job["id"] + ":2")):
            message = git(self.knowledge, "show", "-s", "--format=%B", commit).splitlines()
            self.assertEqual([line for line in message if line.startswith("AKF-Job:")], ["AKF-Job: " + marker])
        saved = self.note.read_bytes()
        for update in candidate["updates"]:
            self.assertIn(update["content"].encode(), saved)
            self.assertEqual(saved.count(("<!-- akf:" + update["claim_id"] + " -->").encode()), 1)
        self.assert_synced(self.engine.submit(job["id"], completed), 3)
        self.assert_synced(self.engine.resume(job["id"]), 3)
        self.assertEqual(self.note.read_bytes(), saved)

    def assert_partial_recovery(self, failure):
        job = self.job()
        candidate = self.mixed(job)
        original = git_sync._run
        def interrupted(root, *args, **kwargs):
            if failure == "push" and args[0] == "push":
                return subprocess.CompletedProcess(args, 128, "", "synthetic local transport failure")
            result = original(root, *args, **kwargs)
            if failure == "commit" and args[0] == "commit":
                raise git_sync.SyncError("retry", "Synthetic interruption after commit")
            return result
        with patch("akf.git_sync._run", side_effect=interrupted):
            first = self.engine.submit(job["id"], candidate)
        self.assertEqual(first["state"], "retry", first)
        self.assertEqual(first["write_state"], "committed" if failure == "push" else "local_saved")
        self.assertEqual(first["pending_count"], 1)
        local = git(self.knowledge, "rev-parse", "HEAD")
        self.assertNotEqual(local, self.baseline)
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/main"), self.baseline)
        self.assertEqual(git(self.knowledge, "rev-list", "--count", "HEAD"), "2")
        saved = self.note.read_bytes()
        self.engine.db.close()
        self.engine = Engine(self.config)
        self.addCleanup(self.engine.db.close)
        recovered = self.engine.resume(job["id"])
        self.assert_synced(recovered, 2)
        self.assertEqual(recovered["commit"], local)
        self.assertEqual(recovered["pending_count"], 1)
        self.assertEqual(self.note.read_bytes(), saved)
        self.assert_synced(self.engine.resume(job["id"]), 2)
        self.assertEqual(self.note.read_bytes(), saved)

    def test_push_failure_resumes_same_partial_commit(self):
        self.assert_partial_recovery("push")

    def test_interruption_after_commit_resumes_same_partial_commit(self):
        self.assert_partial_recovery("commit")

    def test_undo_all_batches_preserves_human_text_and_withdraws_unwritten_pending(self):
        job = self.job()
        candidate = self.mixed(job, deployment=True)
        self.assert_synced(self.engine.submit(job["id"], candidate), 2)
        completed = self.complete_test(job, candidate["updates"][2])
        second = self.engine.submit(job["id"], completed)
        self.assert_synced(second, 3)
        self.assertEqual(second["pending_count"], 1)
        human_addition = "\nA person added this after both batches.\n"
        self.note.write_text(self.note.read_text() + human_addition)
        git(self.knowledge, "add", "--", "topics/limits.md")
        git(self.knowledge, "commit", "-m", "human addition")
        git(self.knowledge, "push", "origin", "main")
        human_commit = git(self.knowledge, "rev-parse", "HEAD")
        undone = self.engine.undo(job["id"])
        self.assert_synced(undone, 5)
        self.assertEqual(git(self.knowledge, "rev-parse", undone["commit"] + "^"), human_commit)
        text = self.note.read_text()
        self.assertTrue(text.startswith(self.human))
        self.assertTrue(text.endswith(human_addition))
        self.assertNotIn("<!-- akf:", text)
        for update in candidate["updates"]:
            self.assertNotIn(update["content"], text)
        self.assertEqual(self.engine.status(job["id"])["pending_count"], 0)
        saved = self.note.read_bytes()
        self.engine.resume(job["id"])
        self.assert_synced(self.engine.undo(job["id"]), 5)
        self.assertEqual(self.note.read_bytes(), saved)

    def test_human_edit_after_partial_save_conflicts_without_remote_change(self):
        job = self.job()
        candidate = self.mixed(job)
        first = self.engine.submit(job["id"], candidate)
        self.assert_synced(first, 2)
        completed = self.complete_test(job, candidate["updates"][2])
        self.note.write_text(self.note.read_text() + "\nUncommitted human paragraph.\n")
        saved = self.note.read_bytes()
        result = self.engine.submit(job["id"], completed)
        self.assertEqual(result["state"], "conflict", result)
        self.assertEqual(self.note.read_bytes(), saved)
        self.assertEqual(git(self.knowledge, "rev-parse", "HEAD"), first["commit"])
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/main"), first["commit"])
        self.assertEqual(git(self.knowledge, "rev-list", "--count", "HEAD"), "2")
        self.assertEqual(self.engine.status(job["id"])["pending_count"], 1)
        self.assertEqual(self.engine.resume(job["id"])["state"], "conflict")
        self.assertEqual(self.note.read_bytes(), saved)


if __name__ == "__main__":
    unittest.main()
