"""Retained, isolated black-box acceptance starting from a delivered ZIP.

Creates only a new scratch directory. It never registers real-user hooks.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile
import pty
import re
import selectors
import time


def accept(archive, scratch, marketplace=None):
    archive = Path(archive).resolve(strict=True)
    scratch = Path(scratch).absolute()
    scratch.mkdir(mode=0o700)  # Refuse any pre-existing directory.
    private_home = scratch / "home"
    private_home.mkdir()
    env = {**os.environ, "HOME": str(private_home), "AKF_HOME": str(scratch / "support"),
           "AKF_INSTALL_ROOT": str(scratch / "installed-code"), "AKF_BIN_DIR": str(scratch / "bin"),
           "CODEX_HOME": str(scratch / "codex"), "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "PYTHONDONTWRITEBYTECODE": "1"}
    calls = []
    def run(args, cwd=scratch, stdin=None):
        result = subprocess.run([str(x) for x in args], cwd=cwd, env=env, input=stdin,
                                capture_output=True, text=True, timeout=30)
        calls.append({"args": [str(x) for x in args], "cwd": str(cwd), "exit_code": result.returncode})
        (scratch / "calls.json").write_text(json.dumps(calls, ensure_ascii=False, indent=2) + "\n")
        if result.returncode:
            (scratch / "command-failure.log").write_text(result.stdout + result.stderr)
            raise AssertionError(f"Acceptance command failed: {args[0]}; exit {result.returncode}")
        return result.stdout
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        assert all(not Path(i.filename).is_absolute() and ".." not in Path(i.filename).parts for i in entries)
        root = entries[0].filename.split("/")[0]
        launcher_entry = bundle.getinfo(root + "/Install.command")
        assert (launcher_entry.external_attr >> 16) & 0o111
    extracted = scratch / "unpacked"
    run(["/usr/bin/ditto", "-x", "-k", archive, extracted])
    package = extracted / root
    assert archive.name == root + ".zip"
    project = scratch / "合成项目 with spaces"
    knowledge = scratch / "合成知识库 with spaces"
    project.mkdir()
    knowledge.mkdir()
    (project / "src").mkdir()
    (knowledge / "topics").mkdir()
    (knowledge / ".obsidian").mkdir()
    note = knowledge / "topics/human.md"
    note.write_text("# 人工笔记\n\n此段由用户维护。\n")
    obsidian = knowledge / ".obsidian/preferences.json"
    obsidian.write_text('{"synthetic":"preserve"}\n')
    config = project / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text('[hooks]\nSessionStart = [{hooks = [{type = "command", command = "true"}]}]\n')
    hooks = project / ".codex/hooks.json"
    hooks.write_text('{"user_field":5,"hooks":{"Stop":[{"hooks":[{"type":"command","command":"true"}]}]}}\n')
    protected = {str(p): p.read_bytes() for p in (note, obsidian, config)}
    original_hooks = json.loads(hooks.read_bytes())
    for repository in (project, knowledge):
        run(["git", "init", "-b", "main", repository])
        run(["git", "config", "user.name", "Synthetic Fixture"], cwd=repository)
        run(["git", "config", "user.email", "fixture@example.test"], cwd=repository)
        baseline = repository / "baseline.txt"
        baseline.write_text("baseline\n")
        run(["git", "add", "baseline.txt"], cwd=repository)
        run(["git", "commit", "-m", "synthetic baseline"], cwd=repository)
        baseline.write_text("staged human change\n")
        run(["git", "add", "baseline.txt"], cwd=repository)
        baseline.write_text("unstaged human change\n")
        (repository / "untracked-human.txt").write_text("untracked human change\n")
    indexes = {str(p): run(["git", "ls-files", "--stage"], cwd=p) for p in (project, knowledge)}
    installer = package / "Install.command"
    choices = ["--non-interactive", "--apply", "--project", project, "--knowledge", knowledge,
               "--sync", "local", "--hook-mode", "direct", "--authorize-update", "no",
               "--authorize-commit", "no", "--authorize-push", "no"]
    master, slave = pty.openpty()
    wizard = subprocess.Popen([str(installer)], cwd=project / "src", env=env, stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    transcript = b""
    selector = selectors.DefaultSelector()
    selector.register(master, selectors.EVENT_READ)
    # Accept the detected Git root, choose a Vault, retain all three denied permissions.
    os.write(master, ("\n" + str(knowledge) + "\n\n\n\n\n\nyes\n\n").encode())
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            if selector.select(.1):
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                transcript += chunk
            elif wizard.poll() is not None:
                break
        wizard.wait(timeout=1)
    finally:
        if wizard.poll() is None:
            wizard.terminate()
            wizard.wait(timeout=2)
        selector.close()
        os.close(master)
        (scratch / "wizard-transcript.log").write_bytes(transcript)
    assert wizard.returncode == 0
    assert all(text.encode() in transcript for text in ("项目目录", "知识库目录", "应用以上改动", "工具安装完成"))
    calls.append({"args": [str(installer)], "cwd": str(project / "src"), "interactive": True,
                  "exit_code": wizard.returncode, "permissions": {"update": False, "commit": False, "push": False}})
    assert not Path(env["CODEX_HOME"]).exists()
    second = run([installer, *choices], cwd=project / "src")
    assert all(json.loads(line)["state"] == "no_op" for line in second.splitlines())
    cli = scratch / "bin/akf"
    doctor = json.loads(run([cli, "doctor"], cwd=project / "src"))
    assert doctor["states"]["Installed"] and doctor["states"]["Configured"]
    assert not doctor["states"]["Enabled"]
    assert doctor["states"]["Sync Enabled"] == {"commit": False, "push": False}
    assert not (scratch / "support/state").exists()
    assert json.loads(run([cli, "managed-hook", "--mode", "direct"], cwd=project,
                         stdin=json.dumps({"cwd": str(project), "hook_event_name": "SessionStart", "session_id": "synthetic"}))) == {}
    uninstall = json.loads(run([cli, "uninstall"], cwd=project))
    assert not cli.exists()
    assert json.loads(run([installer, "uninstall"]))["state"] == "no_op"
    assert json.loads(run([installer, "rollback", "--transaction", uninstall["transaction"]]))["state"] == "rolled_back"
    assert cli.exists()
    run([cli, "uninstall"], cwd=project)
    assert not cli.exists()
    for path, data in protected.items():
        assert Path(path).read_bytes() == data
    final_hooks = json.loads(hooks.read_bytes())
    assert final_hooks["hooks"]["Stop"] == original_hooks["hooks"]["Stop"]
    assert final_hooks["user_field"] == 5
    for repository in (project, knowledge):
        assert run(["git", "ls-files", "--stage"], cwd=repository) == indexes[str(repository)]
        assert (repository / "baseline.txt").read_text() == "unstaged human change\n"
        assert (repository / "untracked-human.txt").read_text() == "untracked human change\n"
    marketplace_result = None
    if marketplace:
        Path(env["CODEX_HOME"]).mkdir(exist_ok=True)
        codex = shutil.which("codex")
        assert codex
        marketplace = Path(marketplace).resolve(strict=True)
        marketplace_name = json.loads((marketplace / ".agents/plugins/marketplace.json").read_bytes())["name"]
        assert isinstance(marketplace_name, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]*", marketplace_name)
        plugin_reference = "agent-knowledge-workflow@" + marketplace_name
        json.loads(run([codex, "plugin", "marketplace", "add", marketplace, "--json"]))
        installed_plugin = False
        try:
            listed = json.loads(run([codex, "plugin", "list", "--marketplace", marketplace_name, "--available", "--json"]))
            assert any(p["name"] == "agent-knowledge-workflow" for p in listed["available"])
            json.loads(run([codex, "plugin", "add", plugin_reference, "--json"]))
            installed_plugin = True
            cache = Path(env["CODEX_HOME"]) / "plugins/cache" / marketplace_name / "agent-knowledge-workflow"
            manifests = list(cache.glob("*/plugin.json")) + list(cache.glob("*/.codex-plugin/plugin.json"))
            assert len(manifests) == 1
            manifest = json.loads(manifests[0].read_bytes())
            onboarding = manifest["extensions"]["com.openai"]["onboardingSkill"]
            manifest_root = manifests[0].parent
            layout = "legacy" if manifest_root.name == ".codex-plugin" else "portable"
            if layout == "legacy":
                manifest_root = manifest_root.parent
            assert (manifest_root / onboarding).is_file()
        finally:
            if installed_plugin:
                run([codex, "plugin", "remove", plugin_reference])
            run([codex, "plugin", "marketplace", "remove", marketplace_name])
        marketplace_result = {"registered": True, "discovered_plugin": True, "manifest_layout": layout,
                              "installed_in_isolated_codex_home": True, "onboarding_file_present": True, "removed": True}
    report = {"archive": str(archive), "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
              "isolation": "temporary HOME, synthetic repositories; not a clean machine or Desktop UI test",
              "result": "PASS", "cli_independent_of_plugin": True, "default_automatic_permissions": False,
              "interactive_single_entry_setup": True,
              "protected_notes_and_git": True, "fixture_entrypoints_removed": True,
              "native_marketplace": marketplace_result, "calls": calls}
    (scratch / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "calls"}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True)
    parser.add_argument("--scratch", required=True)
    parser.add_argument("--marketplace")
    args = parser.parse_args()
    accept(args.archive, args.scratch, args.marketplace)
