import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]


class ReleasePackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="akf-release-中文 space-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update({"HOME": str(self.root / "home"),
                         "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                         "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "Synthetic Builder",
                         "GIT_AUTHOR_EMAIL": "builder@example.test", "GIT_COMMITTER_NAME": "Synthetic Builder",
                         "GIT_COMMITTER_EMAIL": "builder@example.test", "PYTHONDONTWRITEBYTECODE": "1",
                         "SOURCE_DATE_EPOCH": "1700000000"})
        Path(self.env["HOME"]).mkdir()
        fixture = {
            "akf/__init__.py": '__version__ = "0.2.0"\n',
            "akf/__main__.py": "pass\n",
            "akf/cli.py": "pass\n",
            "akf/install.py": "pass\n",
            "Install.command": "#!/bin/sh\nexit 0\n",
            "hooks/hooks.json": '{"hooks": {}}\n',
            "skills/setup/SKILL.md": "# Synthetic setup fixture\n",
            "scripts/python.sh": "#!/bin/sh\nexit 0\n",
            "scripts/install.py": "pass\n",
            "scripts/plugin_hook.sh": "#!/bin/sh\nexit 0\n",
            "scripts/plugin_entry.py": "pass\n",
            "docs/install.md": "# Synthetic installation fixture\n",
            "docs/reproducible-build.md": "# Synthetic build fixture\n",
            "README.md": "# Synthetic package fixture\n",
            "LICENSE": "Synthetic license fixture for packaging tests.\n",
            "SECURITY.md": "# Synthetic security fixture\n",
            "CHANGELOG.md": "# Synthetic changelog fixture\n",
            "CONTRIBUTING.md": "# Synthetic contribution fixture\n",
            "synthetic-knowledge/topics/project-behavior.md": "# Synthetic project behavior\n\nFixture knowledge only.\n",
            ".gitignore": ".local/\n",
            ".local/private-note.md": "Synthetic local material must stay outside the package.\n",
            ".env": "SYNTHETIC_FIXTURE=excluded\n",
            "docs/private-notes.md": "Synthetic unlisted document.\n",
        }
        for name, content in fixture.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        (self.source / "plugin.json").write_text(json.dumps({
            "name": "agent-knowledge-workflow", "version": "0.2.0",
            "description": "Synthetic plugin fixture",
            "extensions": {"com.openai": {"onboardingSkill": "./skills/setup/SKILL.md",
                                            "hooks": "./hooks/hooks.json"}},
        }), encoding="utf-8")
        shutil.copyfile(ROOT / "scripts/build_package.py", self.source / "scripts/build_package.py")
        self.git("init", "-b", "main")
        self.git("add", ".")
        self.git("commit", "-m", "synthetic package fixture")

    def git(self, *arguments):
        result = subprocess.run(["git", "-c", "core.hooksPath=" + str(self.root / "no-hooks"),
                                 "-c", "commit.gpgsign=false", "-C", str(self.source), *arguments],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def build(self, output, *, release=True, umask=0o022, epoch=None):
        command = [sys.executable, "-B", str(self.source / "scripts/build_package.py"), "--output", str(output)]
        if release:
            command.append("--release")
        environment = dict(self.env)
        if epoch is not None:
            environment["SOURCE_DATE_EPOCH"] = epoch
        return subprocess.run(command, cwd=self.source, env=environment, capture_output=True,
                              text=True, timeout=20, umask=umask)

    def artifact_hash(self, path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def test_release_reproduces_bytes_and_binds_both_layouts_to_committed_source(self):
        first = self.build(self.root / "first", umask=0o022)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        for directory, folders, names in os.walk(self.source):
            folders[:] = [name for name in folders if name != ".git"]
            for name in names:
                os.utime(Path(directory) / name, (1800000000, 1800000000))
        second = self.build(self.root / "second", umask=0o077)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        left, right = json.loads(first.stdout), json.loads(second.stdout)
        self.assertEqual(Path(left["archive"]).name, "TokemulaAgentKnowledgeWorkflow-0.2.0-macos.zip")
        self.assertEqual(Path(left["marketplace_archive"]).name, "TokemulaAgentKnowledgeWorkflow-0.2.0-codex-plugin.zip")
        for key in ("archive", "marketplace_archive"):
            self.assertEqual(Path(left[key]).read_bytes(), Path(right[key]).read_bytes(), key)
        self.assertEqual((self.root / "first/SHA256SUMS").read_bytes(),
                         (self.root / "second/SHA256SUMS").read_bytes())
        sums = dict(line.split("  ", 1)[::-1]
                    for line in (self.root / "first/SHA256SUMS").read_text().splitlines())
        for key in ("archive", "marketplace_archive"):
            path = Path(left[key])
            self.assertEqual(sums[path.name], self.artifact_hash(path))
        package = Path(left["package"])
        plugin = Path(left["marketplace"]) / "plugins/agent-knowledge-workflow"
        canonical = json.loads((package / "plugin.json").read_bytes())
        legacy = json.loads((plugin / ".codex-plugin/plugin.json").read_bytes())
        self.assertFalse((plugin / "plugin.json").exists())
        self.assertFalse((package / ".codex-plugin/plugin.json").exists())
        self.assertEqual(legacy["hooks"], canonical["extensions"]["com.openai"]["hooks"])
        self.assertEqual(legacy["extensions"]["com.openai"]["onboardingSkill"],
                         canonical["extensions"]["com.openai"]["onboardingSkill"])
        catalog = json.loads((Path(left["marketplace"]) / ".agents/plugins/marketplace.json").read_bytes())
        self.assertEqual(catalog["name"], "agent-knowledge-workflow")
        self.assertEqual(catalog["interface"]["displayName"], "Tokemula Agent Knowledge Workflow")
        manifests = []
        for directory in (package, plugin):
            manifest = json.loads((directory / "PACKAGE.json").read_bytes())
            manifests.append(manifest)
            self.assertEqual(manifest["source_commit"], self.git("rev-parse", "HEAD"))
            self.assertEqual(manifest["source_tree"], self.git("rev-parse", "HEAD^{tree}"))
            self.assertFalse(manifest["source_dirty"])
            self.assertFalse(manifest["private_candidate"])
            self.assertIn("LICENSE", manifest["files"])
            self.assertIn("README.md", manifest["files"])
            self.assertIn("SECURITY.md", manifest["files"])
            self.assertIn("synthetic-knowledge/topics/project-behavior.md", manifest["files"])
            self.assertNotIn(".env", manifest["files"])
            self.assertNotIn("docs/private-notes.md", manifest["files"])
            self.assertNotIn(".local/private-note.md", manifest["files"])
            self.assertNotIn(str(self.root), json.dumps(manifest))
            for name, record in manifest["files"].items():
                self.assertEqual(record["sha256"], self.artifact_hash(directory / name))
        shared = manifests[0]["files"].keys() & manifests[1]["files"].keys()
        for name in shared:
            self.assertEqual((package / name).read_bytes(), (plugin / name).read_bytes(), name)
        for key in ("archive", "marketplace_archive"):
            with zipfile.ZipFile(left[key]) as bundle:
                self.assertEqual(bundle.namelist(), sorted(bundle.namelist()))
                for entry in bundle.infolist():
                    self.assertEqual(entry.date_time, time.gmtime(1700000000)[:6])
                    self.assertEqual(entry.create_system, 3)
                    self.assertEqual(entry.compress_type, zipfile.ZIP_STORED)
                    mode = (entry.external_attr >> 16) & 0o777
                    self.assertEqual(mode, 0o755 if entry.filename.endswith((".sh", ".command")) else 0o644)
                    self.assertNotIn(str(self.root).encode(), bundle.read(entry))

    def test_release_rejects_dirty_source_and_clean_source_without_license(self):
        (self.source / "akf/cli.py").write_text("# synthetic dirty change\npass\n")
        dirty = self.build(self.root / "dirty")
        self.assertNotEqual(dirty.returncode, 0)
        self.assertFalse(list((self.root / "dirty").glob("*.zip")))
        self.git("add", "akf/cli.py")
        self.git("commit", "-m", "synthetic updated fixture")
        (self.source / "LICENSE").unlink()
        self.git("add", "LICENSE")
        self.git("commit", "-m", "synthetic missing license fixture")
        unlicensed = self.build(self.root / "unlicensed")
        self.assertNotEqual(unlicensed.returncode, 0)
        self.assertIn("LICENSE", unlicensed.stdout + unlicensed.stderr)
        self.assertFalse(list((self.root / "unlicensed").glob("*.zip")))

    def test_candidate_marks_dirty_source_without_claiming_release(self):
        (self.source / "akf/cli.py").write_text("# candidate change\npass\n")
        result = self.build(self.root / "candidate", release=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifest = json.loads((Path(json.loads(result.stdout)["package"]) / "PACKAGE.json").read_bytes())
        self.assertTrue(manifest["private_candidate"])
        self.assertTrue(manifest["source_dirty"])
        self.assertEqual(manifest["source_commit"], self.git("rev-parse", "HEAD"))
        self.assertEqual(manifest["files"]["akf/cli.py"]["sha256"],
                         self.artifact_hash(self.source / "akf/cli.py"))

    def test_release_rejects_payload_not_matching_head_even_when_status_is_clean(self):
        ignored = self.source / "akf/private_fixture.py"
        with (self.source / ".git/info/exclude").open("a") as stream:
            stream.write("\nakf/private_fixture.py\n")
        ignored.write_text("# Synthetic ignored payload must not become a release.\n")
        self.assertEqual(self.git("status", "--porcelain"), "")
        with self.subTest(case="ignored untracked payload"):
            output = self.root / "ignored-release"
            result = self.build(output)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(list(output.glob("*.zip")))
        with self.subTest(case="ignored candidate remains private"):
            candidate = self.build(self.root / "ignored-candidate", release=False)
            self.assertEqual(candidate.returncode, 0, candidate.stdout + candidate.stderr)
            manifest = json.loads((Path(json.loads(candidate.stdout)["package"]) / "PACKAGE.json").read_bytes())
            self.assertTrue(manifest["private_candidate"])
            self.assertIn("akf/private_fixture.py", manifest["files"])
        ignored.unlink()
        self.git("update-index", "--assume-unchanged", "akf/cli.py")
        (self.source / "akf/cli.py").write_text("# Synthetic hidden tracked change\npass\n")
        self.assertEqual(self.git("status", "--porcelain"), "")
        with self.subTest(case="assume-unchanged tracked payload"):
            output = self.root / "hidden-change-release"
            result = self.build(output)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(list(output.glob("*.zip")))

    def test_invalid_source_date_epoch_fails_before_producing_an_archive(self):
        for index, epoch in enumerate(("not-an-integer", "-1", "4354819200")):
            with self.subTest(epoch=epoch):
                output = self.root / ("bad-epoch-" + str(index))
                result = self.build(output, epoch=epoch)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SOURCE_DATE_EPOCH", result.stdout + result.stderr)
                self.assertFalse(list(output.glob("*.zip")))


if __name__ == "__main__":
    unittest.main()
