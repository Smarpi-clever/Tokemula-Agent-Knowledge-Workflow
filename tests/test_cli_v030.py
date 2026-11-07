"""Black-box v0.3 flows use private homes, synthetic sources, and captured exits."""
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".local/v0.3.0/acceptance"
TRACE = []


class CliV030Tests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="cli-中文 space-", dir=SCRATCH)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.knowledge = self.root / "knowledge"
        self.project.mkdir()
        (self.knowledge / "topics").mkdir(parents=True)
        (self.root / "home").mkdir()
        self.source = self.project / "settings.py"
        self.source.write_text("LIMIT = 4\n", encoding="utf-8")
        (self.project / "test_settings.py").write_text(
            "import unittest\nfrom settings import LIMIT\n"
            "class Limits(unittest.TestCase):\n"
            "    def test_positive(self): self.assertGreater(LIMIT, 0)\n", encoding="utf-8")
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "knowledge_id": "cli-acceptance", "project_root": str(self.project),
            "knowledge_root": str(self.knowledge), "state_dir": str(self.root / "state"),
            "enabled": True, "environment": "synthetic", "project_snapshot": False,
            "git": {"auto_commit": False, "auto_push": False},
        }), encoding="utf-8")
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update({"HOME": str(self.root / "home"), "CODEX_HOME": str(self.root / "codex"),
                         "AKF_HOME": str(self.root / "support"), "AKF_INSTALL_ROOT": str(self.root / "tool"),
                         "AKF_BIN_DIR": str(self.root / "bin"), "GIT_CONFIG_GLOBAL": os.devnull,
                         "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
                         "PYTHONDONTWRITEBYTECODE": "1"})
        for repository in (self.project, self.knowledge):
            subprocess.run(["git", "init", "--quiet", "-b", "main", str(repository)],
                           env=self.env, check=True, capture_output=True)

    def cli(self, *arguments, payload=None, expect=0):
        process = subprocess.run([sys.executable, "-B", str(ROOT / "akf/__main__.py"),
                                  "--config", str(self.config), "--json", *arguments],
                                 input=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                                 capture_output=True, text=True, cwd=self.project, env=self.env, timeout=20)
        self.assertEqual(process.returncode, expect, process.stdout + process.stderr)
        result = json.loads(process.stdout)
        TRACE.append({"test": self.id(), "arguments": list(arguments), "exit_code": process.returncode,
                      "result": result})
        return result

    def hook(self, name, turn="task", **fields):
        return self.cli("hook", payload={"hook_event_name": name, "cwd": str(self.project),
                                         "session_id": "acceptance", "turn_id": turn, **fields})

    def job(self, turn="task"):
        self.hook("UserPromptSubmit", turn, prompt="Check the synthetic request limit.")
        process = subprocess.run([sys.executable, "-B", "-m", "unittest", "-q"], cwd=self.project,
                                 env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("Ran 1 test", process.stderr)
        hint = self.hook("PostToolUse", turn, tool_use_id="test-" + turn, tool_name="Bash",
                         tool_input={"cmd": "python3 -B -m unittest -q"},
                         tool_response={"exit_code": process.returncode})
        self.assertIn("additionalContext", hint["hookSpecificOutput"])
        prepared = self.cli("prepare", "--session", "acceptance", "--turn", turn)
        self.assertIn("job", prepared)
        return prepared["job"]

    def update(self, *, content="请求上限为四。", claim="limit", claim_type="file_fact"):
        note = self.knowledge / "topics/limits.md"
        return {"topic": "topics/limits.md", "title": "运行设置", "claim_id": claim,
                "claim_type": claim_type, "content": content,
                "expected_sha256": hashlib.sha256(note.read_bytes()).hexdigest() if note.exists() else None,
                "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "evidence": [{"kind": "file", "path": "settings.py",
                              "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
                              "excerpt": self.source.read_text().strip()}]}

    def test_search_read_update_search_and_local_completion(self):
        note = self.knowledge / "topics/limits.md"
        note.write_text("# 运行设置\n\n人工说明保留。\n", encoding="utf-8")
        first = self.job()
        saved = self.cli("submit", "--job", first["id"],
                         payload={"status": "completed", "updates": [self.update()]})
        self.assertEqual(saved["state"], "local_saved")
        found = self.cli("search", "--query", "请求上限", "--budget-bytes", "6000")
        self.assertTrue(found["results"])
        self.assertIn("请求上限为四", json.dumps(found, ensure_ascii=False))
        self.assertEqual(found["results"][0]["evidence_status"], "tool_verified")
        detail = self.cli("read", "--path", "topics/limits.md", "--budget-bytes", "6000")
        self.assertIn("LIMIT = 4", json.dumps(detail, ensure_ascii=False))
        self.assertIn("settings.py", json.dumps(detail, ensure_ascii=False))
        self.source.write_text("LIMIT = 12\n", encoding="utf-8")
        next_job = self.job("update")
        updated = self.cli("submit", "--job", next_job["id"], payload={
            "status": "completed", "updates": [self.update(content="请求上限为十二。")],
        })
        self.assertEqual(updated["state"], "local_saved")
        current = self.cli("search", "--query", "请求上限")
        self.assertIn("请求上限为十二", json.dumps(current, ensure_ascii=False))
        self.assertNotIn("请求上限为四", json.dumps(current, ensure_ascii=False))
        historical = self.cli("search", "--query", "请求上限", "--history")
        self.assertTrue(any(item["lifecycle"] == "superseded" for item in historical["results"]))
        context = self.cli("context", "--task", "请求上限")
        self.assertEqual(context["pending_jobs"], [])
        self.assertIn("人工说明保留。", note.read_text())

    def test_pending_item_review_revalidation_preserves_saved_sibling(self):
        job = self.job()
        source = self.update()
        tests = self.update(claim="limit-tests", claim_type="test_result", content="合成 limit 测试通过。")
        tests["evidence"] = []
        deployment = self.update(claim="limit-deployment", claim_type="deployment", content="合成部署候选。")
        deployment["evidence"] = []
        result = self.cli("submit", "--job", job["id"], payload={
            "status": "partial", "updates": [source, tests, deployment],
        })
        self.assertEqual(result["state"], "local_saved")
        self.assertEqual(result["pending_count"], 2)
        note = self.knowledge / "topics/limits.md"
        saved = note.read_bytes()
        explained = self.cli("explain", "--job", job["id"])
        self.assertEqual(len(explained["items"]), 2)
        self.assertTrue(all(item["evidence_gap"] for item in explained["items"]))
        pending_search = self.cli("search", "--query", "合成部署候选")
        self.assertTrue(any(item["source_origin"] == "pending" for item in pending_search["results"]))
        rejected = self.cli("review", "--job", job["id"], "--item", "topics/limits.md:limit-deployment",
                            "--action", "reject")
        self.assertEqual(rejected["pending_count"], 1)
        self.assertEqual(note.read_bytes(), saved)
        confirmed = self.cli("review", "--job", job["id"], "--item", "topics/limits.md:limit-tests",
                             "--action", "confirm")
        self.assertEqual(confirmed["pending_count"], 1)
        self.assertEqual(note.read_bytes(), saved)
        completed_test = copy.deepcopy(tests)
        completed_test["evidence"] = [{"kind": "receipt", "receipt_id": job["receipts"][0]["id"],
                                      "expected_exit": 0}]
        reviewed = self.cli("review", "--job", job["id"], "--item", "topics/limits.md:limit-tests",
                            "--action", "revalidate", payload=completed_test)
        self.assertEqual(reviewed["pending_count"], 0)
        state = self.cli("status", "--job", job["id"])
        self.assertEqual(state["pending_count"], 0)
        self.assertIn("请求上限为四", note.read_text())
        self.assertIn("合成 limit 测试通过", note.read_text())
        self.assertEqual(self.cli("context")["pending_jobs"], [])

    def test_human_edit_delete_rebuild_and_no_results(self):
        note = self.knowledge / "topics/manual.md"
        note.write_text("# 运行手记\n\n请求去重使用幂等键。\n", encoding="utf-8")
        self.assertTrue(self.cli("search", "--query", "幂等键")["results"])
        note.write_text("# 运行手记\n\n故障标记 ERR_STREAM_RESET 位于 src/channel.py。\n", encoding="utf-8")
        self.assertEqual(self.cli("search", "--query", "幂等键")["results"], [])
        mixed = self.cli("search", "--query", "故障 ERR_STREAM_RESET")
        self.assertTrue(mixed["results"])
        self.assertEqual(mixed["results"][0]["source_origin"], "human_note")
        self.assertEqual(self.cli("index", "--rebuild")["state"], "rebuilt")
        self.assertTrue(self.cli("search", "--query", "src/channel.py")["results"])
        bounded = self.cli("search", "--query", "故障", "--budget-bytes", "512")
        self.assertLessEqual(len(json.dumps(bounded, ensure_ascii=False, sort_keys=True).encode()), 512)
        note.unlink()
        self.assertEqual(self.cli("search", "--query", "ERR_STREAM_RESET")["results"], [])
        self.assertEqual(self.cli("search", "--query", "unrelated-nebula-94273")["results"], [])

    def test_isolated_hook_entrypoint_is_quiet_and_reference_only(self):
        note = self.knowledge / "topics/manual.md"
        note.write_text("# PRIVATE_SYNTHETIC_BODY\n\nKeep this body out of Hook context.\n", encoding="utf-8")
        started = self.hook("SessionStart")
        context = started["hookSpecificOutput"]["additionalContext"]
        self.assertIn("context", context)
        self.assertIn("reference data", context)
        self.assertNotIn("PRIVATE_SYNTHETIC_BODY", context)
        self.hook("UserPromptSubmit", prompt="No durable new knowledge.")
        self.assertEqual(self.hook("Stop"), {})
        self.assertFalse((self.root / "codex").exists())


if __name__ == "__main__":
    unittest.main()
