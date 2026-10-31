import argparse
import base64
import contextlib
import copy
import datetime as dt
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from akf.install import Manager, InstallError, encoded, read_json, sha, discover_vaults, undo_json
from akf.setup_cli import inspect, managed_hook

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".local/tmp"
SCRATCH.mkdir(parents=True, exist_ok=True)


class InstallationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.artifacts = tempfile.TemporaryDirectory(prefix="package-", dir=SCRATCH)
        result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/build_package.py"), "--output", cls.artifacts.name],
                                capture_output=True, text=True, check=True)
        built = json.loads(result.stdout)
        cls.package = Path(built["package"])
        cls.archive = Path(built["archive"])
        cls.plugin_package = Path(built["marketplace"]) / "plugins/agent-knowledge-workflow"

    @classmethod
    def tearDownClass(cls):
        cls.artifacts.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="install-中文 space-", dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        support = self.home / ("Library/Application Support/AgentKnowledgeWorkflow" if sys.platform == "darwin"
                               else ".local/state/agent-knowledge-workflow")
        self.env = patch.dict(os.environ, {"HOME": str(self.home), "AKF_HOME": str(support),
                                          "AKF_INSTALL_ROOT": str(self.root / "code"), "AKF_BIN_DIR": str(self.root / "bin"),
                                          "CODEX_HOME": str(self.root / "codex"), "GIT_CONFIG_GLOBAL": os.devnull,
                                          "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "PYTHONDONTWRITEBYTECODE": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.manager = Manager()
        with self.manager.lock():
            self.install = self.manager.install_package(self.package)
        self.project = self.root / "合成 project"
        self.project.mkdir()
        self.knowledge = self.root / "合成 knowledge"
        self.knowledge.mkdir()
        self.note = self.knowledge / "human.md"
        self.note.write_text("人工笔记保持完整。\n")

    def setup_mapping(self, mode="direct", grants=None, **options):
        with self.manager.lock():
            return self.manager.setup(self.project, self.knowledge, "local", mode,
                                      grants or {"update": False, "commit": False, "push": False}, **options)

    def command(self, *args, input=None, cwd=None):
        return subprocess.run([str(self.manager.bin), *args], cwd=cwd or self.project,
                              input=input, capture_output=True, text=True, timeout=20)

    def ownership_expiry(self, minutes):
        return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)).isoformat()

    def ownership_update(self, engine, value, *, receipts=True, pending=False):
        from akf.engine import now
        source = engine.project_root / "settings.py"
        note = engine.knowledge_root / "topics/limits.md"
        if not source.exists():
            source.write_text("LIMIT = 0\n")
        turn = "value-" + str(value)
        engine.start_turn("ownership-session", turn, "a" * 64)
        source.write_text(f"LIMIT = {value}\n")
        if receipts:
            engine.record_receipt("ownership-session", turn, "call-" + turn, "Bash",
                                  {"cmd": "cat settings.py"}, {"exit_code": 0})
        job = engine.prepare_maintenance("ownership-session", turn)
        self.assertIsNotNone(job)
        update = {"topic": "topics/limits.md", "title": "Limits", "claim_id": "request-limit",
                  "claim_type": "file_fact", "content": f"The limit is {value}.",
                  "expected_sha256": sha(note.read_bytes()) if note.exists() else None,
                  "observed_at": now(), "evidence": [{"kind": "file", "path": "settings.py",
                  "sha256": sha(source.read_bytes()), "excerpt": source.read_text().strip()}]}
        updates = [update]
        if pending:
            updates.append({**update, "claim_id": "missing-proof", "claim_type": "test_result",
                            "content": "Unverified: the test outcome needs a receipt.", "evidence": []})
        return engine.submit(job["id"], {"status": "completed", "updates": updates})

    def ownership_fixture(self, environment="local"):
        from akf.engine import Engine
        grants = {"update": True, "commit": False, "push": False}
        first = self.setup_mapping(grants=grants, expires_at=self.ownership_expiry(60))
        mapping = self.manager.registry()["mappings"][first["mapping"]]
        config_path = Path(mapping["config_path"])
        if environment != "local":
            config = read_json(config_path)
            config["environment"] = environment
            config_path.write_bytes(encoded(config))
            registry = self.manager.registry()
            registry["mappings"][mapping["id"]]["config_sha256"] = sha(config_path.read_bytes())
            self.manager.registry_path.write_bytes(encoded(registry))
        config = read_json(config_path)
        note = self.knowledge / "topics/limits.md"
        note.parent.mkdir()
        note.write_text("# Limits\n\nHuman introduction stays.\n")
        engine = Engine(config_path)
        try:
            pending = self.ownership_update(engine, 8, receipts=True, pending=True)
            self.assertEqual(pending["state"], "local_saved")
            self.assertEqual(pending["pending_count"], 1)
            self.assertEqual(self.ownership_update(engine, 12, receipts=True)["state"], "local_saved")
        finally:
            engine.db.close()
        return {"mapping": mapping, "config": config, "config_path": config_path,
                "db": Path(config["state_dir"]) / "state.sqlite3", "note": note, "grants": grants}

    def legacy_ownership_fixture(self):
        import shlex
        from akf.engine import Engine
        config_path = self.root / "legacy-config.json"
        config = {"knowledge_id": "legacy-ownership", "project_root": str(self.project),
                  "knowledge_root": str(self.knowledge), "state_dir": str(self.root / "legacy-state"),
                  "environment": "local", "enabled": True, "expires_at": self.ownership_expiry(60),
                  "git": {"auto_commit": False, "auto_push": False}}
        config_path.write_bytes(encoded(config))
        note = self.knowledge / "topics/limits.md"
        note.parent.mkdir()
        note.write_text("# Limits\n\nLegacy human introduction stays.\n")
        engine = Engine(config_path)
        try:
            result = self.ownership_update(engine, 8, receipts=True, pending=True)
            self.assertEqual(result["state"], "local_saved")
            self.assertEqual(result["pending_count"], 1)
        finally:
            engine.db.close()
        hook = self.project / ".codex/hooks.json"
        hook.parent.mkdir()
        user_handler = {"type": "command", "command": "true"}
        old_handler = {"type": "command", "command": shlex.join(
            [sys.executable, str(ROOT / "akf/__main__.py"), "--config", str(config_path), "hook"])}
        hook.write_bytes(encoded({"description": "User setup", "hooks": {"Stop": [
            {"custom": "keep", "hooks": [user_handler, old_handler]}]}}))
        return {"config_path": config_path, "db": Path(config["state_dir"]) / "state.sqlite3",
                "note": note, "hook": hook, "grants": {"update": True, "commit": False, "push": False}}

    def test_install_from_package_launcher_without_plugin_or_marketplace(self):
        self.assertFalse(self.manager.codex.exists())
        result = subprocess.run(["/bin/sh", str(self.package / "Install.command"), "--non-interactive", "--apply",
                                 "--project", str(self.project), "--knowledge", str(self.knowledge), "--sync", "local",
                                 "--hook-mode", "direct", "--authorize-update", "no", "--authorize-commit", "no", "--authorize-push", "no"],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.project / ".codex/hooks.json").is_file())
        self.assertFalse(self.manager.codex.exists())
        self.assertEqual(self.note.read_text(), "人工笔记保持完整。\n")
        self.assertFalse((self.manager.home / "state").exists())

    def test_repeated_install_and_setup_noop(self):
        self.assertEqual(self.manager.install_package(self.package)["state"], "no_op")
        first = self.setup_mapping()
        before = (self.project / ".codex/hooks.json").read_bytes()
        second = self.setup_mapping()
        self.assertEqual(second["state"], "no_op")
        self.assertEqual(first["mapping"], second["mapping"])
        self.assertEqual((self.project / ".codex/hooks.json").read_bytes(), before)
        self.assertEqual(len(self.manager.registry()["mappings"]), 1)

    def test_preview_is_readonly_and_apply_rechecks_snapshot(self):
        result = self.setup_mapping(apply=False)
        self.assertEqual(result["state"], "preview")
        self.assertFalse((self.project / ".codex").exists())
        self.assertEqual(self.manager.registry()["mappings"], {})
        path = self.project / ".codex/hooks.json"
        path.parent.mkdir()
        path.write_text('{"hooks": {}, "user": 1}')
        with self.assertRaises(InstallError):
            self.setup_mapping(expected=result["preview_id"])
        self.assertEqual(self.manager.registry()["mappings"], {})

    def test_incremental_hooks_inline_and_unknown_fields_preserved(self):
        hook = self.project / ".codex/hooks.json"
        hook.parent.mkdir()
        original = {"custom": {"keep": 5}, "hooks": {"Stop": [{"matcher": "x", "custom": True,
                     "hooks": [{"type": "command", "command": "true", "extra": "keep"}]}], "FutureEvent": []}}
        hook.write_bytes(encoded(original))
        inline = hook.with_name("config.toml")
        inline.write_text('[hooks]\nSessionStart = [{ hooks = [{ type = "command", command = "true" }] }]\n')
        inline_before = inline.read_bytes()
        result = self.setup_mapping()
        after = read_json(hook)
        self.assertEqual(after["custom"], original["custom"])
        self.assertEqual(after["hooks"]["Stop"][0], original["hooks"]["Stop"][0])
        self.assertEqual(inline.read_bytes(), inline_before)
        after["another_user_field"] = 7
        hook.write_bytes(encoded(after))
        rolled = self.manager.rollback(result["transaction"])
        self.assertEqual(rolled["state"], "rolled_back")
        self.assertEqual(read_json(hook), {**original, "another_user_field": 7})
        self.assertEqual(inline.read_bytes(), inline_before)

    def test_invalid_hooks_preserved(self):
        path = self.project / ".codex/hooks.json"
        path.parent.mkdir()
        path.write_text("broken JSON")
        with self.assertRaises(InstallError):
            self.setup_mapping()
        self.assertEqual(path.read_text(), "broken JSON")
        self.assertEqual(self.manager.registry()["mappings"], {})

    def test_hook_metadata_preserved_and_actual_akf_commands_rejected(self):
        project_inline = self.project / ".codex/config.toml"
        project_inline.parent.mkdir()
        self.manager.codex.mkdir()
        user_inline = self.manager.codex / "config.toml"
        metadata = ('[hooks]\n'
                    'SessionStart = [{hooks = [{type = "command", command = "true", statusMessage = "AKF history"}]}]\n'
                    '[hooks.state]\n'
                    '"/synthetic/Agent-Knowledge-Workflow/hooks" = { enabled = false }\n')
        for path in (project_inline, user_inline):
            path.write_text(metadata)
        project_json = project_inline.with_name("hooks.json")
        user_json = user_inline.with_name("hooks.json")
        document = {"description": "Historical AKF documentation", "hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": "true", "statusMessage": "AKF history"}]}]}}
        for path in (project_json, user_json):
            path.write_bytes(encoded(document))
        untouched = {p: p.read_bytes() for p in (project_inline, user_inline, user_json)}

        self.setup_mapping()
        self.assertEqual({p: p.read_bytes() for p in untouched}, untouched)
        self.assertEqual(read_json(project_json)["description"], document["description"])
        self.assertEqual(read_json(project_json)["hooks"]["SessionStart"][0], document["hooks"]["SessionStart"][0])
        protected = {p: p.read_bytes() for p in (project_inline, user_inline, project_json, user_json)}
        registry = self.manager.registry_path.read_bytes()
        transactions = list((self.manager.home / "transactions").iterdir())
        for path in protected:
            for field in ("command", "commandWindows", "command_windows"):
                handler = {"type": "command", "command": "true", field: "/synthetic/akf managed-hook"}
                if path.suffix == ".toml":
                    added = '\n[[hooks.Stop]]\n[[hooks.Stop.hooks]]\n' + ''.join(
                        key + ' = ' + json.dumps(value) + '\n' for key, value in handler.items())
                    path.write_bytes(protected[path] + added.encode())
                else:
                    modified = read_json(path)
                    modified["hooks"].setdefault("Stop", []).append({"hooks": [handler]})
                    path.write_bytes(encoded(modified))
                try:
                    with self.subTest(path=path, field=field), self.assertRaisesRegex(InstallError, "AKF Hook"):
                        self.setup_mapping(grants={"update": True, "commit": False, "push": False})
                finally:
                    path.write_bytes(protected[path])
                self.assertEqual(self.manager.registry_path.read_bytes(), registry)
                self.assertEqual(list((self.manager.home / "transactions").iterdir()), transactions)

    def test_unowned_legacy_hooks_refuse_overwrite(self):
        path = self.project / ".codex/hooks.json"
        path.parent.mkdir()
        path.write_bytes(encoded({"hooks": {"Stop": [{"hooks": [{"command": "python /legacy/akf/__main__.py hook"}]}]}}))
        before = path.read_bytes()
        with self.assertRaises(InstallError):
            self.setup_mapping()
        self.assertEqual(path.read_bytes(), before)

    def test_owned_registration_switch_has_one_active_path(self):
        first = self.setup_mapping()
        second = self.setup_mapping(mode="plugin")
        registry = self.manager.registry()
        self.assertTrue(registry["mappings"][first["mapping"]]["archived"])
        self.assertNotEqual(first["mapping"], second["mapping"])
        self.assertFalse(read_json(Path(registry["mappings"][first["mapping"]]["config_path"]))["enabled"])
        for groups in read_json(self.project / ".codex/hooks.json")["hooks"].values():
            self.assertEqual(groups, [])

    def test_changed_binding_uses_new_state_pending_retained(self):
        first = self.setup_mapping(grants={"update": True, "commit": False, "push": False})
        old = self.manager.registry()["mappings"][first["mapping"]]
        config = read_json(Path(old["config_path"]))
        state = Path(config["state_dir"])
        state.mkdir(parents=True)
        (state / "pending.json").write_text("retained")
        other = self.root / "other-knowledge"
        other.mkdir()
        with self.manager.lock():
            second = self.manager.setup(self.project, other, "local", "direct", {"update": False, "commit": False, "push": False})
        new = self.manager.registry()["mappings"][second["mapping"]]
        self.assertNotEqual(config["state_dir"], read_json(Path(new["config_path"]))["state_dir"])
        self.assertEqual((state / "pending.json").read_text(), "retained")

    def test_missing_authorizations_not_replaced_by_apply(self):
        result = self.command("setup", "--non-interactive", "--apply", "--project", str(self.project))
        self.assertEqual(result.returncode, 1)
        self.assertIn("--authorize-update", result.stdout)
        self.assertEqual(self.manager.registry()["mappings"], {})

    def test_default_hook_quiet_and_no_state(self):
        self.setup_mapping()
        event = {"hook_event_name": "SessionStart", "cwd": str(self.project), "session_id": "synthetic"}
        result = self.command("managed-hook", "--mode", "direct", input=json.dumps(event))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout), {})
        self.assertFalse((self.manager.home / "state").exists())
        self.assertEqual(self.note.read_text(), "人工笔记保持完整。\n")

    def test_unmatched_plugin_does_not_create_installation_or_state(self):
        empty_home = self.root / "empty"
        with patch.dict(os.environ, {"AKF_HOME": str(empty_home)}):
            result = self.command("managed-hook", "--mode", "plugin", input=json.dumps({"cwd": str(self.project), "hook_event_name": "Stop"}))
        self.assertEqual(json.loads(result.stdout), {})
        self.assertFalse(empty_home.exists())

    def test_explicit_config_business_commands_still_work_and_close(self):
        config = self.root / "explicit.json"
        config.write_bytes(encoded(dict(knowledge_id="explicit", project_root=str(self.project),
                                       knowledge_root=str(self.knowledge), state_dir=str(self.root / "legacy-state"), enabled=False)))
        result = self.command("--config", str(config), "status")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout), {"enabled": False, "jobs": []})
        self.assertEqual(self.command("--config", str(config), "resume", "--job", "missing").returncode, 1)

    def test_pause_disable_enable_and_uninstall_retains_data(self):
        first = self.setup_mapping(grants={"update": True, "commit": False, "push": False})
        mapping = self.manager.registry()["mappings"][first["mapping"]]
        config = Path(mapping["config_path"])
        for action in ("pause", "enable", "disable", "enable"):
            result = self.command(action)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(read_json(config)["enabled"], action == "enable")
        result = self.manager.uninstall()
        self.assertFalse(self.manager.bin.exists())
        self.assertFalse(read_json(config)["enabled"])
        self.assertTrue(config.exists())
        self.assertEqual(self.note.read_text(), "人工笔记保持完整。\n")
        self.assertEqual(self.manager.uninstall()["state"], "no_op")
        self.assertEqual(self.manager.rollback(result["transaction"])["state"], "rolled_back")
        self.assertTrue(self.manager.bin.exists())

    def test_enable_cannot_grant_update(self):
        first = self.setup_mapping()
        with self.assertRaises(InstallError):
            self.manager.control(first["mapping"], "enable")

    def test_uninstall_after_hook_file_removed_preserves_absence_and_data(self):
        first = self.setup_mapping(grants={"update": True, "commit": False, "push": False})
        mapping = self.manager.registry()["mappings"][first["mapping"]]
        config = Path(mapping["config_path"])
        state = Path(read_json(config)["state_dir"])
        state.mkdir(parents=True)
        pending = state / "pending.json"
        pending.write_text("retained pending work\n")
        hooks = self.project / ".codex/hooks.json"
        hooks.unlink()

        result = self.manager.uninstall()
        self.assertEqual(result["state"], "applied")
        self.assertFalse(self.manager.registry()["installed"])
        self.assertTrue(self.manager.registry()["mappings"][first["mapping"]]["disabled"])
        self.assertFalse(read_json(config)["enabled"])
        self.assertFalse(self.manager.bin.exists())
        self.assertFalse(hooks.exists())
        self.assertEqual(self.manager.uninstall()["state"], "no_op")

        self.assertEqual(self.manager.rollback(result["transaction"])["state"], "rolled_back")
        self.assertTrue(self.manager.registry()["installed"])
        self.assertTrue(self.manager.bin.exists())
        self.assertTrue(read_json(config)["enabled"])
        self.assertFalse(hooks.exists())
        self.assertEqual(pending.read_text(), "retained pending work\n")
        self.assertEqual(self.note.read_text(), "人工笔记保持完整。\n")

    def test_reinstall_restores_owned_hooks_with_same_or_new_expiry(self):
        import datetime as dt
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=20)).isoformat()
        later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=40)).isoformat()
        hooks = self.project / ".codex/hooks.json"
        hooks.parent.mkdir()
        user_group = {"hooks": [{"type": "command", "command": "true"}]}
        hooks.write_bytes(encoded({"description": "User configuration", "hooks": {"Stop": [user_group]}}))
        grants = {"update": True, "commit": False, "push": False}
        self.setup_mapping(expires_at=expiry, grants=grants)
        for selected_expiry in (expiry, later):
            with self.subTest(expiry=selected_expiry):
                old = self.manager.select(self.project)
                config = Path(old["config_path"])
                old_config = read_json(config)
                state = Path(old_config["state_dir"])
                state.mkdir(parents=True, exist_ok=True)
                pending = state / "pending.json"
                pending.write_text("retained pending work\n")
                with self.manager.lock():
                    self.manager.uninstall()
                    self.manager.install_package(self.package)
                result = self.setup_mapping(expires_at=selected_expiry, grants=grants)
                self.assertEqual(result["state"], "applied")
                current = self.manager.select(self.project)
                current_config = read_json(Path(current["config_path"]))
                self.assertEqual(current_config["expires_at"], selected_expiry)
                self.assertTrue(current_config["enabled"])
                self.assertFalse(current["disabled"])
                if selected_expiry == old_config["expires_at"]:
                    self.assertEqual(current["id"], old["id"])
                    self.assertEqual(current_config["state_dir"], old_config["state_dir"])
                else:
                    self.assertNotEqual(current["id"], old["id"])
                    self.assertTrue(self.manager.registry()["mappings"][old["id"]]["archived"])
                self.assertTrue(config.exists())
                self.assertEqual(pending.read_text(), "retained pending work\n")
                document = read_json(hooks)
                self.assertEqual(document["description"], "User configuration")
                self.assertEqual(document["hooks"]["Stop"][0], user_group)
                for event, group in self.manager.hook_group().items():
                    self.assertEqual(document["hooks"][event].count(group), 1)
                self.assertEqual(self.setup_mapping(expires_at=selected_expiry, grants=grants)["state"], "no_op")
                self.assertEqual(self.note.read_text(), "人工笔记保持完整。\n")

    def test_active_hook_changes_still_block_same_or_changed_setup(self):
        import datetime as dt
        self.setup_mapping()
        hooks = self.project / ".codex/hooks.json"
        original = read_json(hooks)
        registry = self.manager.registry_path.read_bytes()
        transactions = list((self.manager.home / "transactions").iterdir())
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=20)).isoformat()
        for tamper in ("removed", "edited"):
            document = copy.deepcopy(original)
            if tamper == "removed":
                document["hooks"]["Stop"] = []
            else:
                document["hooks"]["Stop"][0]["hooks"][0]["command"] += " user-edited"
            hooks.write_bytes(encoded(document))
            before = hooks.read_bytes()
            for options in ({}, {"expires_at": expiry}):
                with self.subTest(tamper=tamper, options=options), self.assertRaises(InstallError):
                    self.setup_mapping(**options)
                self.assertEqual(hooks.read_bytes(), before)
                self.assertEqual(self.manager.registry_path.read_bytes(), registry)
                self.assertEqual(list((self.manager.home / "transactions").iterdir()), transactions)

    def test_no_engine_import_for_management_without_config(self):
        script = 'import sys; from akf.cli import main; main(["doctor"]); assert "akf.engine" not in sys.modules'
        result = subprocess.run([sys.executable, "-B", "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_concurrent_manager_operation_refused(self):
        with self.manager.lock():
            with self.assertRaises(InstallError):
                with Manager().lock():
                    self.fail("Second manager must not acquire lock")

    def test_same_name_launcher_is_preserved(self):
        self.manager.bin.write_text("user-owned executable")
        with self.assertRaises(InstallError):
            self.manager.install_package(self.package)
        self.assertEqual(self.manager.bin.read_text(), "user-owned executable")

    def test_package_tamper_rejected_before_switch(self):
        damaged = self.root / "damaged"
        shutil.copytree(self.package, damaged)
        (damaged / "akf/cli.py").write_text("tampered")
        before = (self.manager.code / "current.json").read_bytes()
        with self.assertRaises(InstallError):
            self.manager.install_package(damaged)
        self.assertEqual((self.manager.code / "current.json").read_bytes(), before)

    def test_upgrade_and_rollback(self):
        other = self.root / "upgrade"
        shutil.copytree(self.package, other)
        manifest = read_json(other / "PACKAGE.json")
        manifest["version"] = "0.2.1-test"
        (other / "PACKAGE.json").write_bytes(encoded(manifest))
        before = self.manager.registry()["package"]
        upgraded = self.command("upgrade", "--from", str(other))
        self.assertEqual(upgraded.returncode, 0, upgraded.stdout)
        self.assertNotEqual(self.manager.registry()["package"], before)
        transaction = json.loads(upgraded.stdout)["transaction"]
        self.assertEqual(self.command("rollback", "--transaction", transaction).returncode, 0)
        self.assertEqual(self.manager.registry()["package"], before)

    def test_partial_transaction_recovers_without_touching_user_file(self):
        a, b = self.root / "a.json", self.root / "b.json"
        a.write_bytes(encoded({"keep": 1}))
        import akf.install as service
        original = service.restore
        failed = False
        def interrupted(path, value):
            nonlocal failed
            if path == b and not failed:
                failed = True
                raise KeyboardInterrupt()
            return original(path, value)
        with patch("akf.install.restore", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.manager.transaction([(a, encoded({"keep": 2}), 0o600), (b, b"new", 0o600)], "test")
        self.assertEqual(read_json(a), {"keep": 1})
        self.assertFalse(b.exists())
        self.assertEqual(self.note.read_text(), "人工笔记保持完整。\n")

    def test_rollback_conflict_preserves_all_current_files(self):
        first = self.setup_mapping()
        hook = self.project / ".codex/hooks.json"
        doc = read_json(hook)
        doc["hooks"]["Stop"][0]["hooks"][0]["command"] += " modified-by-user"
        hook.write_bytes(encoded(doc))
        registry_before = self.manager.registry_path.read_bytes()
        result = self.manager.rollback(first["transaction"])
        self.assertEqual(result["state"], "rollback_conflict")
        self.assertEqual(read_json(hook), doc)
        self.assertEqual(self.manager.registry_path.read_bytes(), registry_before)

    def test_discovery_reads_metadata_not_notes(self):
        registration = self.home / "Library/Application Support/obsidian/obsidian.json"
        registration.parent.mkdir(parents=True)
        registration.write_bytes(encoded({"vaults": {"a": {"path": str(self.knowledge)}}}))
        self.note.chmod(0)
        try:
            found = discover_vaults([self.root])
            self.assertIn(str(self.knowledge), found)
        finally:
            self.note.chmod(0o600)
        self.assertFalse((self.knowledge / ".obsidian").exists())

    def test_overlapping_projects_are_rejected_without_changing_existing_mapping(self):
        first = self.setup_mapping()
        child = self.project / "nested"
        child.mkdir()
        registry = self.manager.registry_path.read_bytes()
        hook = self.project / ".codex/hooks.json"
        original_hook = hook.read_bytes()
        transactions = list((self.manager.home / "transactions").iterdir())
        for project in (child, self.project.parent):
            for overlap in (False, True):
                with self.subTest(project=project, overlap=overlap), self.assertRaisesRegex(InstallError, "重叠"):
                    self.manager.setup(project, self.knowledge, "local", "direct",
                                       {"update": False, "commit": False, "push": False}, overlap=overlap)
        self.assertEqual(self.manager.registry_path.read_bytes(), registry)
        self.assertEqual(hook.read_bytes(), original_hook)
        self.assertEqual(list((self.manager.home / "transactions").iterdir()), transactions)
        self.assertFalse((child / ".codex").exists())
        self.assertEqual(self.manager.select(child)["id"], first["mapping"])

    def test_custom_support_allows_direct_but_rejects_plugin_without_writes(self):
        with patch.dict(os.environ, {"AKF_HOME": str(self.root / "custom-support"),
                                    "AKF_INSTALL_ROOT": str(self.root / "custom-code"),
                                    "AKF_BIN_DIR": str(self.root / "custom-bin")}):
            manager = Manager()
            grants = {"update": False, "commit": False, "push": False}
            with manager.lock():
                manager.install_package(self.package)
                first = manager.setup(self.project, self.knowledge, "local", "direct", grants)
                config = Path(manager.registry()["mappings"][first["mapping"]]["config_path"])
                hook = self.project / ".codex/hooks.json"
                before = {p: p.read_bytes() for p in (manager.registry_path, config, hook)}
                transactions = list((manager.home / "transactions").iterdir())
                for apply in (False, True):
                    with self.subTest(apply=apply), self.assertRaisesRegex(InstallError, "默认.*direct"):
                        manager.setup(self.project, self.knowledge, "local", "plugin", grants, apply=apply)
                self.assertEqual({p: p.read_bytes() for p in before}, before)
                self.assertEqual(list((manager.home / "transactions").iterdir()), transactions)
                self.assertEqual(manager.select(self.project)["hook_mode"], "direct")
                self.assertEqual(self.note.read_text(), "人工笔记保持完整。\n")

    def test_doctor_reports_five_independent_states_and_no_state_write(self):
        first = self.setup_mapping()
        mapping = self.manager.registry()["mappings"][first["mapping"]]
        from akf.native import NativeClient
        native = {"trusted": True, "active": True, "state": "已信任", "hashes": ["a"]}
        with patch("akf.native.NativeClient") as client, patch("akf.native.hook_status", return_value=native):
            result = inspect(self.manager, mapping)
        self.assertEqual(result["states"], {"Installed": True, "Configured": True, "Trusted": "已信任",
                                           "Enabled": False, "Sync Enabled": {"commit": False, "push": False}})
        self.assertFalse((self.manager.home / "state").exists())

    def test_new_hook_file_rollback_keeps_later_user_hook(self):
        first = self.setup_mapping()
        hook = self.project / ".codex/hooks.json"
        doc = read_json(hook)
        user = {"hooks": [{"type": "command", "command": "true"}]}
        doc["hooks"]["Stop"].append(user)
        hook.write_bytes(encoded(doc))
        result = self.manager.rollback(first["transaction"])
        self.assertEqual(result["state"], "rolled_back")
        self.assertEqual(read_json(hook)["hooks"]["Stop"], [user])

    def test_damaged_managed_config_stops_hook_before_engine(self):
        first = self.setup_mapping(grants={"update": True, "commit": False, "push": False})
        mapping = self.manager.registry()["mappings"][first["mapping"]]
        config = read_json(Path(mapping["config_path"]))
        config["git"]["auto_push"] = True
        Path(mapping["config_path"]).write_bytes(encoded(config))
        result = self.command("managed-hook", "--mode", "direct", input=json.dumps({"cwd": str(self.project), "session_id": "s", "hook_event_name": "SessionStart"}))
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.manager.home / "state").exists())

    def test_missing_python_and_old_python_show_same_recovery_entry(self):
        fake = self.root / "old python"
        fake.write_text("#!/bin/sh\nexit 1\n")
        fake.chmod(0o755)
        before = self.manager.registry_path.read_bytes()
        for interpreter in (str(fake), str(self.root / "missing-python")):
            with patch.dict(os.environ, {"AKF_PYTHON": interpreter}):
                result = subprocess.run(["/bin/sh", str(self.package / "Install.command")], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertIn("https://www.python.org/downloads/macos/", result.stdout)
            self.assertIn("Install.command", result.stdout)
        self.assertEqual(self.manager.registry_path.read_bytes(), before)

    def test_upgrade_interruption_restores_version_and_launcher(self):
        import akf.install as service
        other = self.root / "upgrade-interrupted"
        shutil.copytree(self.package, other)
        manifest = read_json(other / "PACKAGE.json")
        manifest["version"] = "0.2.1-interrupted"
        (other / "PACKAGE.json").write_bytes(encoded(manifest))
        before = (self.manager.code / "current.json").read_bytes()
        original = service.restore
        failed = False
        def interrupted(path, value):
            nonlocal failed
            if path == self.manager.code / "current.json" and not failed:
                failed = True
                raise KeyboardInterrupt()
            return original(path, value)
        with patch("akf.install.restore", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.manager.install_package(other)
        self.assertEqual((self.manager.code / "current.json").read_bytes(), before)
        self.assertEqual(self.command("--help").returncode, 0)

    def test_native_trust_revocation_and_plugin_disabled_clear_enabled_display(self):
        first = self.setup_mapping(mode="plugin", grants={"update": True, "commit": False, "push": False})
        registry = self.manager.registry()
        mapping = registry["mappings"][first["mapping"]]
        mapping.update(verified_package=registry["package_id"], verified_hooks=["hash"],
                       verified_since=max(Path(mapping["config_path"]).stat().st_mtime, (self.manager.code / "current.json").stat().st_mtime))
        self.manager.registry_path.write_bytes(encoded(registry))
        with patch("akf.native.NativeClient"), patch("akf.native.hook_status", return_value={"trusted": True, "active": True, "state": "已信任", "hashes": ["hash"]}):
            self.assertTrue(inspect(self.manager, mapping)["states"]["Enabled"])
        for native in ({"trusted": False, "active": True, "state": "待审查", "hashes": ["new"]},
                       {"trusted": True, "active": False, "state": "已信任", "hashes": ["hash"]}):
            with patch("akf.native.NativeClient"), patch("akf.native.hook_status", return_value=native):
                self.assertFalse(inspect(self.manager, mapping)["states"]["Enabled"])

    def test_packaged_engine_roundtrip_local_remote_no_duplicate_or_human_change(self):
        def git(root, *args):
            return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE, text=True).strip()
        git(self.knowledge, "init", "-b", "main")
        git(self.knowledge, "config", "user.name", "Synthetic Fixture")
        git(self.knowledge, "config", "user.email", "fixture@example.test")
        git(self.knowledge, "add", "human.md")
        git(self.knowledge, "commit", "-m", "fixture baseline")
        remote = self.root / "synthetic.git"
        subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True, check=True)
        git(self.knowledge, "remote", "add", "origin", str(remote))
        git(self.knowledge, "push", "origin", "main")
        with self.manager.lock():
            first = self.manager.setup(self.project, self.knowledge, "push", "direct", {"update": True, "commit": True, "push": True}, "origin")
        source = self.project / "settings.py"
        source.write_text("LIMIT = 4\n")
        def event(name, **fields):
            result = self.command("managed-hook", "--mode", "direct", input=json.dumps({"cwd": str(self.project),
                                  "session_id": "synthetic-session", "turn_id": "synthetic-turn", "hook_event_name": name, **fields}))
            self.assertEqual(result.returncode, 0, result.stdout)
            return json.loads(result.stdout)
        event("UserPromptSubmit", prompt="Change the synthetic limit to eight.")
        source.write_text("LIMIT = 8\n")
        event("PostToolUse", tool_use_id="synthetic-write", tool_name="Write",
              tool_input={"file_path": str(source)}, tool_response={})
        stop = event("Stop", stop_hook_active=False)
        identity = stop["reason"].split("[AKF-MAINTENANCE:", 1)[1].split("]", 1)[0]
        from akf.engine import now
        payload = {"status": "completed", "updates": [{"topic": "topics/limits.md", "title": "Limits", "claim_id": "limit",
                   "claim_type": "file_fact", "content": "The synthetic limit is eight.", "expected_sha256": None,
                   "observed_at": now(), "evidence": [{"kind": "file", "path": "settings.py", "sha256": sha(source.read_bytes()), "excerpt": "LIMIT = 8"}]}]}
        result = self.command("submit", "--job", identity, input=json.dumps(payload))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout)["state"], "remote_confirmed")
        head = git(self.knowledge, "rev-parse", "HEAD")
        self.assertEqual(git(remote, "rev-parse", "refs/heads/main"), head)
        self.assertEqual(self.command("resume", "--job", identity).returncode, 0)
        self.assertEqual(git(self.knowledge, "rev-parse", "HEAD"), head)
        self.assertEqual(event("Stop", stop_hook_active=False), {})
        self.assertEqual(self.note.read_text(), "人工笔记保持完整。\n")
        self.command("pause")
        self.assertEqual(event("UserPromptSubmit", prompt="Paused task"), {})

    def test_legacy_migration_requires_specific_config_and_preserves_old_state(self):
        import shlex
        config = self.root / "old-config.json"
        old = dict(knowledge_id="old", project_root=str(self.project), knowledge_root=str(self.knowledge),
                   state_dir=str(self.root / "old-state"), enabled=False)
        config.write_bytes(encoded(old))
        hook = self.project / ".codex/hooks.json"
        hook.parent.mkdir()
        original = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "true"},
                      {"type": "command", "command": shlex.join(["python3", str(self.package / "akf/__main__.py"), "--config", str(config), "hook"])}], "unknown": 1}]}}
        hook.write_bytes(encoded(original))
        with self.assertRaises(InstallError):
            self.setup_mapping()
        result = self.setup_mapping(legacy_config=config)
        self.assertEqual(read_json(hook)["hooks"]["Stop"][0], {"hooks": [{"type": "command", "command": "true"}], "unknown": 1})
        self.assertEqual(read_json(config), old)
        self.assertEqual(self.manager.rollback(result["transaction"])["state"], "rolled_back")
        self.assertEqual(read_json(hook), original)

    def test_installed_pointer_user_change_is_preserved(self):
        pointer = self.manager.code / "current.json"
        pointer.write_bytes(encoded({"path": "user-owned"}))
        before = pointer.read_bytes()
        with self.assertRaises(InstallError):
            self.manager.install_package(self.package)
        with self.assertRaises(InstallError):
            self.manager.uninstall()
        self.assertEqual(pointer.read_bytes(), before)

    def test_expired_authorization_quietly_skips_engine(self):
        import datetime as dt
        expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)).isoformat()
        self.setup_mapping(grants={"update": True, "commit": False, "push": False}, expires_at=expires)
        with patch("akf.setup_cli.config_active", return_value=False):
            with patch("sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps({"cwd": str(self.project), "session_id": "s", "hook_event_name": "SessionStart"}).encode()))):
                self.assertEqual(managed_hook(argparse.Namespace(mode="direct"), self.manager), {})
        self.assertFalse((self.manager.home / "state").exists())

    def test_native_verification_requires_current_saved_write_and_hookprompt_entry(self):
        import sqlite3
        from akf.engine import now
        from akf.setup_cli import verify_job
        first = self.setup_mapping(grants={"update": True, "commit": False, "push": False})
        mapping = self.manager.registry()["mappings"][first["mapping"]]
        config = self.manager.config(mapping)
        state = Path(config["state_dir"])
        state.mkdir(parents=True)
        topic = self.knowledge / "topics/verified.md"
        topic.parent.mkdir()
        topic.write_text("Verified synthetic content\n")
        job = {"id": "job", "created_at": now(), "session_id": "session", "turn_id": "task",
               "maintenance_turn_id": "maintenance", "prompt_marker": "[AKF-MAINTENANCE:job]",
               "write_state": "local_saved", "plans": [{"topic": "topics/verified.md", "after_sha256": sha(topic.read_bytes())}]}
        with sqlite3.connect(state / "state.sqlite3") as db:
            db.execute("CREATE TABLE state (kind TEXT, key TEXT, value TEXT)")
            db.execute("INSERT INTO state VALUES (?,?,?)", ("job", "job", json.dumps(job)))
        class Client:
            item_type = "hookPrompt"
            def request(inner, method, params):
                self.assertEqual(method, "thread/items/list")
                self.assertEqual(params["turnId"], "maintenance")
                return {"data": [{"turnId": "maintenance", "item": {"type": inner.item_type,
                         "fragments": [{"text": job["prompt_marker"] + "\nContinue", "hookRunId": "native-run"}]}}]}
        client = Client()
        self.assertEqual(verify_job(client, mapping)["native_hook_run"], "native-run")
        client.item_type = "userMessage"
        self.assertIsNone(verify_job(client, mapping))
        client.item_type = "hookPrompt"
        self.assertIsNone(verify_job(client, mapping, since=9999999999))
        topic.write_text("Human changed the note\n")
        self.assertIsNone(verify_job(client, mapping))

    def test_older_install_rollback_retains_dependent_configuration(self):
        self.setup_mapping()
        before = self.manager.registry_path.read_bytes()
        result = self.manager.rollback(self.install["transaction"])
        self.assertEqual(result["state"], "rollback_conflict")
        self.assertEqual(self.manager.registry_path.read_bytes(), before)
        self.assertTrue(self.manager.bin.exists())

    def test_launcher_retains_installation_paths_without_shell_environment(self):
        first = self.setup_mapping(grants={"update": True, "commit": False, "push": False})
        env = {k: v for k, v in os.environ.items() if k not in {"AKF_HOME", "AKF_INSTALL_ROOT", "AKF_BIN_DIR"}}
        result = subprocess.run([str(self.manager.bin), "context"], cwd=self.project, env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["knowledge_id"], "akf-" + first["mapping"])
        mapping = self.manager.registry()["mappings"][first["mapping"]]
        package = self.manager.registry()["package"]
        with self.manager.lock():
            result = subprocess.run([sys.executable, "-B", str(Path(package) / "akf/__main__.py"), "--config", mapping["config_path"], "status"],
                                    cwd=self.project, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("正在执行操作", result.stdout)

    def test_unconfigured_plugin_wrapper_is_silent_json_even_without_python(self):
        for interpreter in (sys.executable, str(self.root / "missing-python")):
            empty = self.root / "unconfigured-plugin"
            with patch.dict(os.environ, {"AKF_HOME": str(empty), "PLUGIN_ROOT": str(self.package), "AKF_PYTHON": interpreter}):
                result = subprocess.run(["/bin/sh", str(self.package / "scripts/plugin_hook.sh")], input="{}", capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {})
            self.assertFalse(empty.exists())

    def test_missing_git_or_codex_leaves_configuration_intact_and_can_continue(self):
        from akf.setup_cli import setup
        args = argparse.Namespace(non_interactive=True, project=str(self.project), knowledge=str(self.knowledge),
                                  sync="local", hook_mode="direct", authorize_update="no", authorize_commit="no", authorize_push="no",
                                  apply=True, discover_in=[], remote=None, allow_overlap=False, expires_at=None, migrate_config=None)
        before = self.manager.registry_path.read_bytes()
        for env in ({"git": None, "codex_cli": "installed", "desktop": None},
                    {"git": "installed", "codex_cli": None, "desktop": None}):
            with patch("akf.setup_cli.environment", return_value=env):
                with self.assertRaises(InstallError):
                    setup(args, self.manager)
            self.assertEqual(self.manager.registry_path.read_bytes(), before)
        with patch("akf.setup_cli.environment", return_value={"git": "installed", "codex_cli": "installed", "desktop": None}), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(setup(args, self.manager)["state"], "applied")


    def test_ownership_rebind_preserves_history_without_old_execution_rows(self):
        from akf.engine import Engine
        old = self.ownership_fixture()
        old_bytes = old["db"].read_bytes()
        old["note"].write_text(old["note"].read_text() + "\nNew human paragraph stays.\n")
        registry = self.manager.registry_path.read_bytes()
        expiry = self.ownership_expiry(80)
        preview = self.setup_mapping(grants=old["grants"], expires_at=expiry, apply=False)
        self.assertFalse(Path(preview["state_directory"]).exists())
        self.assertEqual(self.manager.registry_path.read_bytes(), registry)
        self.assertEqual(old["db"].read_bytes(), old_bytes)
        result = self.setup_mapping(grants=old["grants"], expires_at=expiry, expected=preview["preview_id"])
        for value, minutes in ((16, 80), (20, 100)):
            if value == 20:
                expiry = self.ownership_expiry(minutes)
                result = self.setup_mapping(grants=old["grants"], expires_at=expiry)
            mapping = self.manager.select(self.project)
            config = self.manager.config(mapping)
            database = Path(config["state_dir"]) / "state.sqlite3"
            self.assertTrue(database.is_file())
            with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                self.assertEqual(dict(connection.execute("SELECT kind,count(*) FROM state GROUP BY kind")), {"fact": 1})
            engine = Engine(mapping["config_path"])
            try:
                self.assertEqual(engine._all("job"), [])
                self.assertEqual(engine._all("receipt"), [])
                self.assertEqual(self.ownership_update(engine, value)["state"], "local_saved")
            finally:
                engine.db.close()
            self.assertEqual(old["db"].read_bytes(), old_bytes)
        text = old["note"].read_text()
        for value in (8, 12, 16, 20):
            self.assertIn(f"The limit is {value}.", text)
        self.assertIn("New human paragraph stays.", text)
        self.assertEqual(text.count("<!-- akf:request-limit -->"), 1)
        current = database.read_bytes()
        self.assertEqual(self.setup_mapping(grants=old["grants"], expires_at=expiry)["state"], "no_op")
        self.assertEqual(self.manager.rollback(result["transaction"])["state"], "rollback_conflict")
        self.assertEqual(database.read_bytes(), current)
        self.assertEqual(old["note"].read_text(), text)

    def test_ownership_rebind_uses_archives_for_uninitialized_seed_and_empty_current(self):
        from akf.engine import Engine
        old = self.ownership_fixture()
        old_bytes = old["db"].read_bytes()
        self.setup_mapping(grants=old["grants"], expires_at=self.ownership_expiry(80))
        seeded = self.manager.select(self.project)
        seeded_db = Path(self.manager.config(seeded)["state_dir"]) / "state.sqlite3"
        self.assertTrue(seeded_db.is_file())
        seeded_bytes = seeded_db.read_bytes()
        self.setup_mapping(grants=old["grants"], expires_at=self.ownership_expiry(90))
        self.assertEqual(seeded_db.read_bytes(), seeded_bytes)
        empty = self.manager.select(self.project)
        engine = Engine(empty["config_path"])
        # Model a previously released rebind that initialized state without ownership.
        with engine.db:
            engine.db.execute("DELETE FROM state WHERE kind='fact'")
        engine.db.close()
        self.setup_mapping(grants=old["grants"], expires_at=self.ownership_expiry(100))
        engine = Engine(self.manager.select(self.project)["config_path"])
        try:
            self.assertEqual(self.ownership_update(engine, 16)["state"], "local_saved")
        finally:
            engine.db.close()
        self.assertEqual(old["db"].read_bytes(), old_bytes)

    def test_ownership_rebind_rejects_current_fact_rollback_and_block_damage(self):
        from akf.engine import Engine
        old = self.ownership_fixture()
        previous_note = old["note"].read_text()
        self.setup_mapping(grants=old["grants"], expires_at=self.ownership_expiry(80))
        engine = Engine(self.manager.select(self.project)["config_path"])
        try:
            self.assertEqual(self.ownership_update(engine, 16)["state"], "local_saved")
            block = engine._all("fact")[0]["block"]
        finally:
            engine.db.close()
        current_note = old["note"].read_text()
        registry = self.manager.registry_path.read_bytes()
        current_db = Path(self.manager.config(self.manager.select(self.project))["state_dir"]) / "state.sqlite3"
        current_bytes = current_db.read_bytes()
        for name, content in (("older_archive", previous_note),
                              ("edited", current_note.replace("The limit is 16.", "Human changed this fact.")),
                              ("duplicate", current_note + "\n" + block), ("missing", None)):
            with self.subTest(case=name):
                if content is None:
                    old["note"].unlink()
                else:
                    old["note"].write_text(content)
                with self.assertRaises(InstallError):
                    self.setup_mapping(grants=old["grants"], expires_at=self.ownership_expiry(100))
                self.assertEqual(self.manager.registry_path.read_bytes(), registry)
                self.assertEqual(current_db.read_bytes(), current_bytes)
                self.assertEqual(old["note"].read_text() if old["note"].exists() else None, content)
        old["note"].write_text(current_note)

    def test_ownership_rebind_rejects_source_config_and_binding_changes(self):
        old = self.ownership_fixture()
        config_bytes, db_bytes = old["config_path"].read_bytes(), old["db"].read_bytes()
        registry = self.manager.registry_path.read_bytes()
        for corruption in ("config", "binding", "missing_binding"):
            with self.subTest(corruption=corruption):
                if corruption == "config":
                    old["config_path"].write_bytes(config_bytes + b" ")
                else:
                    with contextlib.closing(sqlite3.connect(old["db"])) as connection, connection:
                        if corruption == "binding":
                            connection.execute("UPDATE state SET value=? WHERE kind='meta' AND key='binding'", (json.dumps("wrong"),))
                        else:
                            connection.execute("DELETE FROM state WHERE kind='meta' AND key='binding'")
                changed_config, changed_db = old["config_path"].read_bytes(), old["db"].read_bytes()
                with self.assertRaises(InstallError):
                    self.setup_mapping(grants=old["grants"], expires_at=self.ownership_expiry(80))
                self.assertEqual(self.manager.registry_path.read_bytes(), registry)
                self.assertEqual(old["config_path"].read_bytes(), changed_config)
                self.assertEqual(old["db"].read_bytes(), changed_db)
                old["config_path"].write_bytes(config_bytes)
                old["db"].write_bytes(db_bytes)

    def test_ownership_rebind_refuses_existing_target_and_rolls_back_interruption(self):
        old = self.ownership_fixture()
        registry, old_bytes = self.manager.registry_path.read_bytes(), old["db"].read_bytes()
        expiry = self.ownership_expiry(80)
        preview = self.setup_mapping(grants=old["grants"], expires_at=expiry, apply=False)
        target = Path(preview["state_directory"]) / "state.sqlite3"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"unrelated existing state")
        with self.assertRaises(InstallError):
            self.setup_mapping(grants=old["grants"], expires_at=expiry)
        self.assertEqual(target.read_bytes(), b"unrelated existing state")
        self.assertEqual(self.manager.registry_path.read_bytes(), registry)
        target.unlink()
        target.parent.rmdir()
        import akf.install as service
        restore = service.restore
        failed = False
        def interrupted(path, value):
            nonlocal failed
            if path == self.manager.registry_path and not failed:
                failed = True
                raise OSError("synthetic interruption")
            return restore(path, value)
        with patch("akf.install.restore", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.setup_mapping(grants=old["grants"], expires_at=expiry)
        self.assertEqual(self.manager.registry_path.read_bytes(), registry)
        self.assertEqual(old["db"].read_bytes(), old_bytes)
        self.assertFalse(target.exists())

    def test_ownership_rebind_does_not_cross_project_or_knowledge_roots(self):
        from akf.engine import Engine
        old = self.ownership_fixture()
        old_bytes = old["db"].read_bytes()
        for changed in ("project", "knowledge"):
            with self.subTest(changed=changed):
                project, knowledge = self.project, self.knowledge
                if changed == "project":
                    project = self.root / "other-project"
                    project.mkdir()
                else:
                    knowledge = self.root / "other-knowledge"
                    (knowledge / "topics").mkdir(parents=True)
                    (knowledge / "topics/limits.md").write_bytes(old["note"].read_bytes())
                with self.manager.lock():
                    self.manager.setup(project, knowledge, "local", "direct", old["grants"], expires_at=self.ownership_expiry(80))
                mapping = self.manager.select(project)
                state = Path(self.manager.config(mapping)["state_dir"])
                self.assertFalse(state.exists())
                engine = Engine(mapping["config_path"])
                try:
                    self.assertEqual(engine._all("fact"), [])
                    result = self.ownership_update(engine, 16 if changed == "project" else 20)
                    self.assertEqual(result["error"], "Unknown managed block requires manual review")
                finally:
                    engine.db.close()
                self.assertEqual(old["db"].read_bytes(), old_bytes)

    def test_ownership_rebind_does_not_cross_environment(self):
        from akf.engine import Engine
        old = self.ownership_fixture(environment="synthetic")
        original = old["note"].read_bytes()
        self.setup_mapping(grants=old["grants"], expires_at=self.ownership_expiry(80))
        mapping = self.manager.select(self.project)
        self.assertFalse(Path(self.manager.config(mapping)["state_dir"]).exists())
        engine = Engine(mapping["config_path"])
        try:
            self.assertEqual(engine._all("fact"), [])
            self.assertEqual(self.ownership_update(engine, 16)["error"], "Unknown managed block requires manual review")
        finally:
            engine.db.close()
        self.assertEqual(old["note"].read_bytes(), original)

    def test_ownership_rebind_rejects_invalid_fact_identity_and_symlink(self):
        old = self.ownership_fixture()
        original_db = old["db"].read_bytes()
        registry = self.manager.registry_path.read_bytes()
        with contextlib.closing(sqlite3.connect(old["db"])) as connection:
            key, raw = connection.execute("SELECT key,value FROM state WHERE kind='fact'").fetchone()
        for field, value in (("claim_id", "different-claim"), ("topic", "topics/../escape.md")):
            with self.subTest(field=field):
                fact = json.loads(raw)
                fact[field] = value
                with contextlib.closing(sqlite3.connect(old["db"])) as connection, connection:
                    connection.execute("UPDATE state SET value=? WHERE kind='fact' AND key=?", (json.dumps(fact), key))
                changed = old["db"].read_bytes()
                with self.assertRaises(InstallError):
                    self.setup_mapping(grants=old["grants"], expires_at=self.ownership_expiry(80))
                self.assertEqual(old["db"].read_bytes(), changed)
                self.assertEqual(self.manager.registry_path.read_bytes(), registry)
                old["db"].write_bytes(original_db)
        topics = self.knowledge / "topics"
        outside = self.root / "outside-topics"
        topics.rename(outside)
        topics.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(InstallError):
            self.setup_mapping(grants=old["grants"], expires_at=self.ownership_expiry(80))
        self.assertEqual(self.manager.registry_path.read_bytes(), registry)
        self.assertEqual(old["db"].read_bytes(), original_db)

    def test_ownership_archives_choose_latest_and_reject_same_time_disagreement(self):
        from akf.engine import Engine
        old = self.ownership_fixture()
        self.setup_mapping(grants=old["grants"], expires_at=self.ownership_expiry(80))
        middle = self.manager.select(self.project)
        engine = Engine(middle["config_path"])
        try:
            self.assertEqual(self.ownership_update(engine, 16)["state"], "local_saved")
            newest = engine._all("fact")[0]
        finally:
            engine.db.close()
        self.setup_mapping(grants=old["grants"], expires_at=self.ownership_expiry(100))
        engine = Engine(self.manager.select(self.project)["config_path"])
        with engine.db:
            engine.db.execute("DELETE FROM state WHERE kind='fact'")
        engine.db.close()
        self.assertEqual(self.setup_mapping(grants=old["grants"], expires_at=self.ownership_expiry(120), apply=False)["state"], "preview")
        with contextlib.closing(sqlite3.connect(old["db"])) as connection, connection:
            key, raw = connection.execute("SELECT key,value FROM state WHERE kind='fact'").fetchone()
            fact = json.loads(raw)
            fact["observed_at"] = newest["observed_at"]
            connection.execute("UPDATE state SET value=? WHERE kind='fact' AND key=?", (json.dumps(fact), key))
        registry = self.manager.registry_path.read_bytes()
        with self.assertRaises(InstallError):
            self.setup_mapping(grants=old["grants"], expires_at=self.ownership_expiry(120))
        self.assertEqual(self.manager.registry_path.read_bytes(), registry)


    def test_ownership_seed_survives_same_binding_reinstall_before_initialization(self):
        from akf.engine import Engine
        old = self.ownership_fixture()
        old_bytes = old["db"].read_bytes()
        expiry = self.ownership_expiry(80)
        self.setup_mapping(grants=old["grants"], expires_at=expiry)
        seeded = self.manager.select(self.project)
        ownership = {key: value for key, value in seeded.items() if key.startswith("ownership_")}
        self.assertIn("ownership_seed_sha256", ownership)
        seeded_db = Path(self.manager.config(seeded)["state_dir"]) / "state.sqlite3"
        seed_bytes = seeded_db.read_bytes()
        with self.manager.lock():
            self.manager.uninstall()
            self.manager.install_package(self.package)
        self.setup_mapping(grants=old["grants"], expires_at=expiry)
        restored = self.manager.select(self.project)
        self.assertEqual(restored["id"], seeded["id"])
        self.assertEqual(seeded_db.read_bytes(), seed_bytes)
        self.assertEqual({key: value for key, value in restored.items() if key.startswith("ownership_")}, ownership)
        self.setup_mapping(grants=old["grants"], expires_at=self.ownership_expiry(100))
        engine = Engine(self.manager.select(self.project)["config_path"])
        try:
            self.assertEqual(self.ownership_update(engine, 16)["state"], "local_saved")
        finally:
            engine.db.close()
        self.assertEqual(seeded_db.read_bytes(), seed_bytes)
        self.assertEqual(old["db"].read_bytes(), old_bytes)

    def test_ownership_rebind_to_local_commit_keeps_new_permission_boundary(self):
        from akf.engine import Engine
        old = self.ownership_fixture()
        old_bytes = old["db"].read_bytes()
        human = self.note.read_bytes()
        def git(root, *args):
            return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE, text=True).strip()
        git(self.knowledge, "init", "-b", "main")
        git(self.knowledge, "config", "user.name", "Synthetic Fixture")
        git(self.knowledge, "config", "user.email", "fixture@example.test")
        git(self.knowledge, "add", "--", "topics/limits.md")
        git(self.knowledge, "commit", "-m", "synthetic knowledge baseline")
        baseline = git(self.knowledge, "rev-parse", "HEAD")
        remote = self.root / "ownership-remote.git"
        remote.mkdir()
        git(remote, "init", "--bare")
        git(self.knowledge, "remote", "add", "origin", str(remote))
        grants = {"update": True, "commit": True, "push": False}
        with self.manager.lock():
            self.manager.setup(self.project, self.knowledge, "commit", "direct", grants,
                               remote="origin", expires_at=self.ownership_expiry(80))
        mapping = self.manager.select(self.project)
        config = self.manager.config(mapping)
        self.assertEqual(mapping["grants"], grants)
        self.assertTrue(config["git"]["auto_commit"])
        self.assertFalse(config["git"]["auto_push"])
        self.assertEqual(config["git"]["remote_url"], str(remote))
        engine = Engine(mapping["config_path"])
        try:
            self.assertEqual(engine._all("job"), [])
            self.assertEqual(engine._all("receipt"), [])
            result = self.ownership_update(engine, 16)
            self.assertEqual(result["state"], "committed")
            self.assertEqual(result["commit"], git(self.knowledge, "rev-parse", "HEAD"))
        finally:
            engine.db.close()
        self.assertNotEqual(git(self.knowledge, "rev-parse", "HEAD"), baseline)
        self.assertEqual(git(self.knowledge, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"), "topics/limits.md")
        self.assertEqual(git(remote, "for-each-ref", "refs/heads"), "")
        self.assertEqual(self.note.read_bytes(), human)
        self.assertEqual(old["db"].read_bytes(), old_bytes)
        self.assertIn("The limit is 12.", old["note"].read_text())


    def test_legacy_ownership_migration_preserves_history_and_rollback_boundaries(self):
        from akf.engine import Engine
        old = self.legacy_ownership_fixture()
        protected = {p: p.read_bytes() for p in (old["config_path"], old["db"])}
        old_hooks = old["hook"].read_bytes()
        options = {"grants": old["grants"], "expires_at": self.ownership_expiry(80),
                   "legacy_config": str(old["config_path"])}
        first = self.setup_mapping(**options)
        mapping = self.manager.select(self.project)
        database = Path(self.manager.config(mapping)["state_dir"]) / "state.sqlite3"
        self.assertTrue(database.is_file())
        with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            self.assertEqual(dict(connection.execute("SELECT kind,count(*) FROM state GROUP BY kind")), {"fact": 1})
        self.assertEqual(self.manager.rollback(first["transaction"])["state"], "rolled_back")
        self.assertFalse(database.exists())
        self.assertEqual(old["hook"].read_bytes(), old_hooks)
        self.assertEqual({p: p.read_bytes() for p in protected}, protected)
        second = self.setup_mapping(**options)
        mapping = self.manager.select(self.project)
        engine = Engine(mapping["config_path"])
        try:
            self.assertEqual(engine._all("job"), [])
            self.assertEqual(engine._all("receipt"), [])
            self.assertEqual(engine._all("turn"), [])
            self.assertEqual(self.ownership_update(engine, 12)["state"], "local_saved")
        finally:
            engine.db.close()
        self.assertEqual(read_json(old["hook"])["hooks"]["Stop"][0],
                         {"custom": "keep", "hooks": [{"type": "command", "command": "true"}]})
        self.assertIn("The limit is 8.", old["note"].read_text())
        self.assertIn("Legacy human introduction stays.", old["note"].read_text())
        self.assertEqual({p: p.read_bytes() for p in protected}, protected)
        current_db, current_note = database.read_bytes(), old["note"].read_bytes()
        self.assertEqual(self.manager.rollback(second["transaction"])["state"], "rollback_conflict")
        self.assertEqual(database.read_bytes(), current_db)
        self.assertEqual(old["note"].read_bytes(), current_note)

    def test_explicit_legacy_migration_is_not_skipped_for_identical_current_options(self):
        old = self.legacy_ownership_fixture()
        document = read_json(old["hook"])
        legacy_handler = document["hooks"]["Stop"][0]["hooks"].pop()
        old["hook"].write_bytes(encoded(document))
        expiry = self.ownership_expiry(80)
        self.setup_mapping(grants=old["grants"], expires_at=expiry)
        document = read_json(old["hook"])
        document["hooks"]["Stop"][0]["hooks"].append(legacy_handler)
        old["hook"].write_bytes(encoded(document))
        protected = {p: p.read_bytes() for p in (old["config_path"], old["db"], old["note"])}
        registry, hooks = self.manager.registry_path.read_bytes(), old["hook"].read_bytes()
        options = {"grants": old["grants"], "expires_at": expiry,
                   "legacy_config": str(old["config_path"])}

        preview = self.setup_mapping(**options, apply=False)
        self.assertEqual(preview["state"], "preview")
        self.assertEqual(self.manager.registry_path.read_bytes(), registry)
        self.assertEqual(old["hook"].read_bytes(), hooks)
        self.assertEqual({p: p.read_bytes() for p in protected}, protected)
        self.assertFalse(Path(preview["state_directory"]).exists())
        result = self.setup_mapping(**options, expected=preview["preview_id"])
        self.assertEqual(result["state"], "applied")
        installed = read_json(old["hook"])
        self.assertEqual(installed["hooks"]["Stop"][0],
                         {"custom": "keep", "hooks": [{"type": "command", "command": "true"}]})
        self.assertFalse(any(legacy_handler in group["hooks"]
                             for groups in installed["hooks"].values() for group in groups))
        for event, group in self.manager.hook_group().items():
            self.assertEqual(installed["hooks"][event].count(group), 1)
        self.assertEqual({p: p.read_bytes() for p in protected}, protected)

    def test_legacy_ownership_seed_continues_without_legacy_registry_entry(self):
        from akf.engine import Engine
        old = self.legacy_ownership_fixture()
        protected = {p: p.read_bytes() for p in (old["config_path"], old["db"])}
        self.setup_mapping(grants=old["grants"], expires_at=self.ownership_expiry(80),
                           legacy_config=str(old["config_path"]))
        seeded = self.manager.select(self.project)
        seed_db = Path(self.manager.config(seeded)["state_dir"]) / "state.sqlite3"
        self.assertTrue(seed_db.is_file())
        seed_bytes = seed_db.read_bytes()
        registry = self.manager.registry_path.read_bytes()
        self.assertEqual(len(self.manager.registry()["mappings"]), 1)
        self.assertTrue(all(m["config_path"] != str(old["config_path"])
                            for m in self.manager.registry()["mappings"].values()))
        with contextlib.closing(sqlite3.connect(seed_db)) as connection, connection:
            connection.execute("UPDATE state SET value=? WHERE kind='fact'", (json.dumps({"tampered": True}),))
        tampered = seed_db.read_bytes()
        with self.assertRaises(InstallError):
            self.setup_mapping(grants=old["grants"], expires_at=self.ownership_expiry(100))
        self.assertEqual(seed_db.read_bytes(), tampered)
        self.assertEqual(self.manager.registry_path.read_bytes(), registry)
        seed_db.write_bytes(seed_bytes)
        missing_proof = self.manager.registry()
        missing_proof["mappings"][seeded["id"]].pop("ownership_seed_sha256", None)
        self.manager.registry_path.write_bytes(encoded(missing_proof))
        with self.assertRaises(InstallError):
            self.setup_mapping(grants=old["grants"], expires_at=self.ownership_expiry(100))
        self.assertEqual(seed_db.read_bytes(), seed_bytes)
        self.manager.registry_path.write_bytes(registry)
        self.setup_mapping(grants=old["grants"], expires_at=self.ownership_expiry(100))
        engine = Engine(self.manager.select(self.project)["config_path"])
        try:
            self.assertEqual(self.ownership_update(engine, 16)["state"], "local_saved")
        finally:
            engine.db.close()
        self.assertEqual(seed_db.read_bytes(), seed_bytes)
        self.assertEqual({p: p.read_bytes() for p in protected}, protected)

    def test_legacy_ownership_preview_rechecks_raw_config_even_when_binding_unchanged(self):
        old = self.legacy_ownership_fixture()
        config_bytes = old["config_path"].read_bytes()
        registry = self.manager.registry_path.read_bytes()
        hooks = old["hook"].read_bytes()
        database = old["db"].read_bytes()
        options = {"grants": old["grants"], "expires_at": self.ownership_expiry(80),
                   "legacy_config": str(old["config_path"])}
        for change in ("format", "enabled"):
            with self.subTest(change=change):
                preview = self.setup_mapping(apply=False, **options)
                if change == "format":
                    old["config_path"].write_bytes(config_bytes + b"\n")
                else:
                    config = json.loads(config_bytes)
                    config["enabled"] = False
                    old["config_path"].write_bytes(encoded(config))
                changed = old["config_path"].read_bytes()
                try:
                    with self.assertRaises(InstallError):
                        self.setup_mapping(expected=preview["preview_id"], **options)
                    self.assertEqual(old["config_path"].read_bytes(), changed)
                    self.assertEqual(old["db"].read_bytes(), database)
                    self.assertEqual(self.manager.registry_path.read_bytes(), registry)
                    self.assertEqual(old["hook"].read_bytes(), hooks)
                finally:
                    old["config_path"].write_bytes(config_bytes)

    def test_legacy_ownership_rejects_bad_binding_forged_seed_and_imprecise_hook(self):
        old = self.legacy_ownership_fixture()
        config_bytes, db_bytes = old["config_path"].read_bytes(), old["db"].read_bytes()
        hooks = old["hook"].read_bytes()
        registry = self.manager.registry_path.read_bytes()
        options = {"grants": old["grants"], "expires_at": self.ownership_expiry(80),
                   "legacy_config": str(old["config_path"])}
        for corruption in ("binding", "forged_seed", "hook"):
            with self.subTest(corruption=corruption):
                if corruption == "hook":
                    document = json.loads(hooks)
                    document["hooks"]["Stop"][0]["hooks"][1]["command"] += " --extra"
                    old["hook"].write_bytes(encoded(document))
                else:
                    with contextlib.closing(sqlite3.connect(old["db"])) as connection, connection:
                        if corruption == "binding":
                            connection.execute("UPDATE state SET value=? WHERE kind='meta' AND key='binding'", (json.dumps("wrong"),))
                        else:
                            connection.execute("DELETE FROM state WHERE kind='meta' AND key='binding'")
                    if corruption == "forged_seed":
                        config = json.loads(config_bytes)
                        config["ownership_seed_sha256"] = sha(old["db"].read_bytes())
                        old["config_path"].write_bytes(encoded(config))
                changed = {p: p.read_bytes() for p in (old["config_path"], old["db"], old["hook"])}
                try:
                    with self.assertRaises(InstallError):
                        self.setup_mapping(**options)
                    self.assertEqual({p: p.read_bytes() for p in changed}, changed)
                    self.assertEqual(self.manager.registry_path.read_bytes(), registry)
                finally:
                    old["config_path"].write_bytes(config_bytes)
                    old["db"].write_bytes(db_bytes)
                    old["hook"].write_bytes(hooks)


    def test_plugin_legacy_layout_and_portable_launchers_install_with_defaults_off(self):
        for name, package in (("portable", self.package), ("legacy", self.plugin_package)):
            with self.subTest(layout=name):
                manifest_path = package / ("plugin.json" if name == "portable" else ".codex-plugin/plugin.json")
                self.assertTrue(manifest_path.is_file())
                scope = self.root / (name + "-installation")
                project, knowledge = scope / "project", scope / "knowledge"
                project.mkdir(parents=True)
                knowledge.mkdir()
                note = knowledge / "human.md"
                note.write_text("Keep this human note.\n")
                env = {**os.environ, "AKF_HOME": str(scope / "support"),
                       "AKF_INSTALL_ROOT": str(scope / "code"), "AKF_BIN_DIR": str(scope / "bin"),
                       "CODEX_HOME": str(scope / "codex")}
                result = subprocess.run([str(package / "Install.command"), "--non-interactive", "--apply",
                                         "--project", str(project), "--knowledge", str(knowledge),
                                         "--sync", "local", "--hook-mode", "direct",
                                         "--authorize-update", "no", "--authorize-commit", "no", "--authorize-push", "no"],
                                        env=env, cwd=project, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                with patch.dict(os.environ, env):
                    manager = Manager()
                    registry = manager.registry()
                    mapping = manager.select(project)
                    config = manager.config(mapping)
                    self.assertTrue(registry["installed"])
                    self.assertEqual(registry["version"], read_json(manifest_path)["version"])
                    self.assertTrue(manager.bin.is_file())
                    self.assertEqual(mapping["grants"], {"update": False, "commit": False, "push": False})
                    self.assertFalse(config["enabled"])
                    self.assertEqual(config["git"], {"auto_commit": False, "auto_push": False})
                    self.assertFalse(Path(config["state_dir"]).exists())
                    self.assertFalse(manager.codex.exists())
                self.assertEqual(note.read_text(), "Keep this human note.\n")

    def test_plugin_legacy_layout_derives_metadata_and_binds_package_hashes(self):
        canonical = read_json(self.package / "plugin.json")
        legacy_path = self.plugin_package / ".codex-plugin/plugin.json"
        self.assertTrue(legacy_path.is_file())
        self.assertFalse((self.plugin_package / "plugin.json").exists())
        self.assertFalse((self.package / ".codex-plugin/plugin.json").exists())
        legacy = read_json(legacy_path)
        for key in ("name", "version", "description"):
            self.assertEqual(legacy[key], canonical[key])
        self.assertEqual(legacy["skills"], "./skills/")
        self.assertEqual(legacy["hooks"], canonical["extensions"]["com.openai"]["hooks"])
        self.assertEqual(legacy["extensions"]["com.openai"]["onboardingSkill"],
                         canonical["extensions"]["com.openai"]["onboardingSkill"])
        for package, expected, absent in ((self.package, "plugin.json", ".codex-plugin/plugin.json"),
                                           (self.plugin_package, ".codex-plugin/plugin.json", "plugin.json")):
            manifest = read_json(package / "PACKAGE.json")
            self.assertIn(expected, manifest["files"])
            self.assertNotIn(absent, manifest["files"])
            for name, record in manifest["files"].items():
                self.assertEqual(sha((package / name).read_bytes()), record["sha256"], name)
            self.assertTrue((package / legacy["hooks"]).is_file())
            self.assertTrue((package / legacy["extensions"]["com.openai"]["onboardingSkill"]).is_file())
        with zipfile.ZipFile(self.archive) as bundle:
            prefix = self.package.name + "/"
            self.assertEqual(bundle.read(prefix + "plugin.json"), (self.package / "plugin.json").read_bytes())
            self.assertNotIn(prefix + ".codex-plugin/plugin.json", bundle.namelist())

    def test_plugin_legacy_layout_tampering_or_dual_manifests_preserves_installation(self):
        legacy_manifest = self.plugin_package / ".codex-plugin/plugin.json"
        self.assertTrue(legacy_manifest.is_file())
        pointer = self.manager.code / "current.json"
        protected = {p: p.read_bytes() for p in (pointer, self.manager.registry_path, self.manager.bin)}
        tampered = self.root / "legacy-tampered"
        shutil.copytree(self.plugin_package, tampered)
        (tampered / ".codex-plugin/plugin.json").write_bytes(legacy_manifest.read_bytes() + b" ")
        with self.assertRaisesRegex(InstallError, "校验失败"):
            self.manager.install_package(tampered)
        self.assertEqual({p: p.read_bytes() for p in protected}, protected)
        dual = self.root / "dual-manifest"
        shutil.copytree(self.plugin_package, dual)
        root_manifest = (self.package / "plugin.json").read_bytes()
        (dual / "plugin.json").write_bytes(root_manifest)
        package_manifest = read_json(dual / "PACKAGE.json")
        package_manifest["files"]["plugin.json"] = {"sha256": sha(root_manifest), "mode": 0o644}
        (dual / "PACKAGE.json").write_bytes(encoded(package_manifest))
        with self.assertRaises(InstallError):
            self.manager.install_package(dual)
        self.assertEqual({p: p.read_bytes() for p in protected}, protected)


if __name__ == "__main__":
    unittest.main()
