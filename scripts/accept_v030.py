"""Record isolated v0.3 CLI flows, actual v0.2.2 data compatibility, and Hook probes."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]
BASELINE = "2ac1ee2411e8345ec6043bab907a4b6a5b25af5f"
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))
import test_cli_v030


def compatibility(output):
    source = output / "baseline-source"
    if not (source / "akf/engine.py").is_file():
        source.mkdir(parents=True, exist_ok=True)
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", BASELINE, "akf"],
                                 check=True, capture_output=True)
        subprocess.run(["tar", "-x", "-C", str(source)], input=archive.stdout, check=True)
    fixture = test_cli_v030.CliV030Tests("test_search_read_update_search_and_local_completion")
    fixture.setUp()
    calls = []

    def baseline(code):
        process = subprocess.run([sys.executable, "-B", "-c", code, str(source), str(fixture.config)],
                                 cwd=fixture.project, env=fixture.env, capture_output=True, text=True, timeout=20)
        if process.returncode:
            raise AssertionError(process.stdout + process.stderr)
        result = json.loads(process.stdout)
        calls.append(result)
        return result

    bootstrap = "import sys,json;sys.path.insert(0,sys.argv[1]);from akf.engine import Engine,digest,now;e=Engine(sys.argv[2]);"
    try:
        seed = bootstrap + (
            "e.start_turn('legacy','seed','a'*64);"
            "e.record_receipt('legacy','seed','read','Bash',{'cmd':'cat settings.py'},{'exit_code':0});"
            "j=e.prepare_maintenance('legacy','seed');"
            "u={'topic':'topics/limits.md','title':'运行设置','claim_id':'limit','claim_type':'file_fact',"
            "'content':'请求上限为四。','expected_sha256':None,'observed_at':now(),"
            "'evidence':[{'kind':'file','path':'settings.py','sha256':digest((e.project_root/'settings.py').read_bytes()),'excerpt':'LIMIT = 4'}]};"
            "r=e.submit(j['id'],{'status':'completed','updates':[u]});print(json.dumps(r));e.db.close()"
        )
        seeded = baseline(seed)
        assert seeded["state"] == "local_saved", seeded
        old_search = fixture.cli("search", "--query", "请求上限")
        assert old_search["results"][0]["evidence_status"] == "unknown", old_search
        fixture.source.write_text("LIMIT = 12\n", encoding="utf-8")
        job = fixture.job("upgraded")
        upgraded = fixture.cli("submit", "--job", job["id"], payload={
            "status": "completed", "updates": [fixture.update(content="请求上限为十二。")],
        })
        assert upgraded["state"] == "local_saved", upgraded
        note = fixture.knowledge / "topics/limits.md"
        before_rollback = note.read_bytes()
        reopened = baseline(bootstrap + "print(json.dumps({'context':e.context('请求上限'),'status':e.status()},ensure_ascii=False));e.db.close()")
        assert reopened["context"]["knowledge_id"] == "cli-acceptance", reopened
        assert note.read_bytes() == before_rollback
        assert "请求上限为四" in note.read_text() and "请求上限为十二" in note.read_text()
        rebuilt = fixture.cli("index", "--rebuild")
        assert rebuilt["state"] == "rebuilt", rebuilt
        current = fixture.cli("search", "--query", "请求上限")
        assert "请求上限为十二" in json.dumps(current, ensure_ascii=False), current
        return {"baseline_commit": BASELINE, "baseline_seed": seeded, "upgrade": upgraded,
                "baseline_reopen_after_upgrade": "PASS", "markdown_and_history_preserved": True,
                "index_rebuild_after_rollback": rebuilt, "baseline_calls": calls}
    finally:
        fixture.doCleanups()


def native_query(output):
    scratch = output / "native-stdio"
    scratch.mkdir(parents=True, exist_ok=True)
    home, project = scratch / "home", scratch / "project"
    home.mkdir(exist_ok=True)
    project.mkdir(exist_ok=True)
    (scratch / "codex").mkdir(exist_ok=True)
    hook_root = project / ".codex"
    hook_root.mkdir(exist_ok=True)
    command = shlex.join([sys.executable, "-B", str(ROOT / "akf/__main__.py"),
                          "--config", str(scratch / "synthetic-config.json"), "hook"])
    events = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "Interrupt")
    (hook_root / "hooks.json").write_text(json.dumps({"hooks": {
        event: [{"hooks": [{"type": "command", "command": command}]}] for event in events
    }}), encoding="utf-8")
    env = {"HOME": str(home), "CODEX_HOME": str(scratch / "codex"),
           "AKF_HOME": str(scratch / "support"), "AKF_INSTALL_ROOT": str(scratch / "tool"),
           "AKF_BIN_DIR": str(scratch / "bin")}
    from akf.native import NativeClient
    from akf.install import InstallError
    try:
        with patch.dict(os.environ, env), NativeClient() as client:
            result = client.request("hooks/list", {"cwds": [str(project)]})
        return {"state": "PASS", "protocol": "initialize, initialized, hooks/list", "result": result,
                "discovered_hooks": sum(len(item.get("hooks", [])) for item in result.get("data", [])),
                "hook_trigger_acceptance": "pending_native_trust_and_model_task",
                "model_turns": 0, "native_trust_changes": 0,
                "remaining": "A trusted native model task with synthetic installation remains a release acceptance step."}
    except (InstallError, OSError) as error:
        return {"state": "environment_unavailable", "error": str(error), "model_turns": 0,
                "remaining": "Complete a trusted native task in an authorized model environment before release."}


def package_rollback(output, archive):
    """Switch installed binaries through the existing CLI without touching a real home."""
    source = output / "baseline-package-source"
    source.mkdir(parents=True, exist_ok=True)
    baseline_tree = subprocess.run(["git", "-C", str(ROOT), "archive", BASELINE],
                                   check=True, capture_output=True)
    subprocess.run(["tar", "-x", "-C", str(source)], input=baseline_tree.stdout, check=True)
    package_output = output / "baseline-packages"
    baseline_package = package_output / "TokemulaAgentKnowledgeWorkflow-0.2.2-macos"
    if not baseline_package.is_dir():
        subprocess.run([sys.executable, "-B", str(source / "scripts/build_package.py"),
                        "--output", str(package_output)], check=True, capture_output=True)
    unpacked = output / "candidate-unpacked"
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        assert all(not Path(name).is_absolute() and ".." not in Path(name).parts for name in names)
        roots = {Path(name).parts[0] for name in names}
        assert len(roots) == 1, roots
        bundle.extractall(unpacked)
    candidate = unpacked / roots.pop()
    assert json.loads((candidate / "PACKAGE.json").read_text())["version"] == "0.3.0"
    fixture = test_cli_v030.CliV030Tests("test_search_read_update_search_and_local_completion")
    fixture.setUp()
    calls = []

    def run(arguments, payload=None):
        process = subprocess.run([str(arg) for arg in arguments], cwd=fixture.project, env=fixture.env,
                                 input=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                                 capture_output=True, text=True, timeout=30)
        if process.returncode:
            raise AssertionError(process.stdout + process.stderr)
        result = json.loads(process.stdout)
        calls.append({"args": [str(arg) for arg in arguments], "exit_code": process.returncode, "result": result})
        return result

    try:
        installer = (
            "import sys,json;sys.path.insert(0,sys.argv[1]);from akf.install import Manager;"
            "m=Manager();\nwith m.lock(): r=m.install_package(sys.argv[1])\nprint(json.dumps(r))"
        )
        installed = run([sys.executable, "-B", "-c", installer, baseline_package])
        assert installed["version"] == "0.2.2", installed
        cli = fixture.root / "bin/akf"
        configured = run([cli, "--json", "setup", "--non-interactive", "--apply",
                          "--project", fixture.project, "--knowledge", fixture.knowledge,
                          "--sync", "local", "--hook-mode", "direct", "--authorize-update", "yes",
                          "--authorize-commit", "no", "--authorize-push", "no"])
        registry = json.loads((fixture.root / "support/installation.json").read_text())
        mapping = registry["mappings"][configured["mapping"]]
        fixture.config = Path(mapping["config_path"])

        def command(*args, payload=None):
            return run([cli, "--config", fixture.config, "--json", *args], payload)

        def event(name, **fields):
            return command("hook", payload={"hook_event_name": name, "cwd": str(fixture.project),
                                            "session_id": "package", "turn_id": "legacy", **fields})

        event("UserPromptSubmit", prompt="Inspect the synthetic file and retain a test evidence gap.")
        observed = subprocess.run(["cat", str(fixture.source)], cwd=fixture.project, env=fixture.env,
                                  capture_output=True, text=True, check=True)
        event("PostToolUse", tool_use_id="source", tool_name="Bash", tool_input={"cmd": "cat settings.py"},
              tool_response={"exit_code": observed.returncode})
        job = command("prepare", "--session", "package", "--turn", "legacy")["job"]
        verified, pending = fixture.update(), fixture.update(claim="tests", claim_type="test_result", content="合成测试待核验。")
        pending["evidence"] = []
        seeded = command("submit", "--job", job["id"], payload={"status": "partial", "updates": [verified, pending]})
        assert seeded["state"] == "local_saved" and seeded["pending_count"] == 1, seeded
        note = fixture.knowledge / "topics/limits.md"
        settings = json.loads(fixture.config.read_text())
        database = Path(settings["state_dir"]) / "state.sqlite3"
        protected = {str(path): path.read_bytes() for path in (note, fixture.config, database)}
        upgraded = run([cli, "--json", "upgrade", "--from", candidate])
        assert upgraded["version"] == "0.3.0" and upgraded["state"] == "applied", upgraded
        pointer = fixture.root / "tool/current.json"
        assert json.loads(pointer.read_text())["version"] == "0.3.0"
        found = command("search", "--query", "请求上限")
        assert found["results"] and found["results"][0]["evidence_status"] == "unknown", found
        assert command("status", "--job", job["id"])["pending_count"] == 1
        assert command("index", "--rebuild")["state"] == "rebuilt"
        restored = run([cli, "--json", "rollback", "--transaction", upgraded["transaction"]])
        assert restored["state"] == "rolled_back", restored
        assert json.loads(pointer.read_text())["version"] == "0.2.2"
        old_status = command("status", "--job", job["id"])
        assert old_status["pending_count"] == 1 and old_status["state"] == "local_saved", old_status
        assert all(Path(path).read_bytes() == data for path, data in protected.items())
        rebuilt = fixture.cli("index", "--rebuild")
        assert rebuilt["state"] == "rebuilt", rebuilt
        return {"state": "PASS", "baseline_commit": BASELINE, "installed_baseline": "0.2.2",
                "upgraded_version": "0.3.0", "rolled_back_version": "0.2.2",
                "notes_config_state_and_pending_preserved": True, "index_rebuilt_after_rollback": True,
                "upgrade_transaction": upgraded["transaction"], "calls": calls}
    finally:
        fixture.doCleanups()


def accept(output, archive=None):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if archive:
        report_path = output / "result.json"
        report = json.loads(report_path.read_text()) if report_path.is_file() else {}
        report["package_upgrade_rollback"] = package_rollback(output, Path(archive).resolve(strict=True))
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({key: value for key, value in report["package_upgrade_rollback"].items() if key != "calls"}))
        return 0
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(test_cli_v030.CliV030Tests)
    with (output / "cli-tests.log").open("w", encoding="utf-8") as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    report = {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
              "cli_flows": "PASS" if result.wasSuccessful() else "FAIL",
              "isolation": "Private HOME/CODEX_HOME/AKF paths; synthetic project, notes, and state only."}
    if result.wasSuccessful():
        report["compatibility"] = compatibility(output)
        report["native_stdio"] = native_query(output)
    report["calls"] = test_cli_v030.TRACE
    (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "calls"}, ensure_ascii=False))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(ROOT / ".local/v0.3.0/acceptance"))
    parser.add_argument("--archive", help="Run only the installed 0.2.2-to-0.3.0 upgrade and transaction rollback phase")
    args = parser.parse_args()
    raise SystemExit(accept(args.output, args.archive))
