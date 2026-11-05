"""Human review output retains the facts needed for a decision."""
import contextlib
import io
import shlex
import unittest

from akf.cli import render


class TerminalOutputTests(unittest.TestCase):
    def test_review_shows_target_requirement_and_executable_actions(self):
        output = io.StringIO()
        item = {"item_id": "topics/limits.md:limit", "resolution": "pending", "proposed_change": "replace",
                "candidate": {"content": "请求上限为十二。"},
                "target_fact": {"content": "请求上限为四。", "evidence_status": "tool_verified",
                                "lifecycle": "current", "review_status": "review_required"},
                "sources": [], "evidence_gap": "Missing source evidence", "conflict_reason": None,
                "verification_required": "matching file or fixed commit hash and excerpt",
                "review_command": ["akf", "--config", "/fixture with space/config.json", "review", "--job", "j",
                                   "--item", "topics/limits.md:limit"], "actions": ["show", "revalidate", "reject"]}
        with contextlib.redirect_stdout(output):
            render({"job_id": "j", "state": "pending_evidence", "pending_count": 1, "items": [item]})
        text = output.getvalue()
        self.assertIn("请求上限为四。", text)
        self.assertIn("tool_verified / current / review_required", text)
        self.assertIn(item["verification_required"], text)
        commands = [line.removeprefix("处理：") for line in text.splitlines() if line.startswith("处理：")]
        self.assertEqual(len(commands), 3)
        self.assertEqual(shlex.split(commands[0]), [*item["review_command"], "--action", "show"])
        self.assertIn("< update.json", commands[1])
        self.assertNotIn("|", commands[2])
