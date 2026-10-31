"""Build explicit-file CLI and native-plugin archives without network access."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from akf import __version__

RUNTIME_FILES = ("Install.command", "plugin.json", "hooks/hooks.json", "skills/setup/SKILL.md",
                 "scripts/python.sh", "scripts/install.py", "scripts/plugin_hook.sh",
                 "scripts/plugin_entry.py", "docs/install.md")
PUBLIC_FILES = ("README.md", "LICENSE", "SECURITY.md", "CHANGELOG.md")
OPTIONAL_FILES = ("CONTRIBUTING.md", "docs/privacy.md", "docs/compatibility.md", "docs/dependencies.md",
                  "docs/reproducible-build.md", "examples/README.md", "examples/config.json",
                  "examples/hooks.json", "synthetic-knowledge/topics/project-behavior.md")


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def source_identity(release):
    values = []
    try:
        for args in (("rev-parse", "--show-toplevel"), ("rev-parse", "HEAD"),
                     ("rev-parse", "HEAD^{tree}"), ("show", "-s", "--format=%ct", "HEAD"),
                     ("status", "--porcelain", "--untracked-files=all")):
            result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                                    timeout=10, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
            if result.returncode:
                raise ValueError()
            values.append(result.stdout.strip())
        if Path(values[0]).resolve() != ROOT:
            raise ValueError()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        if release:
            raise SystemExit("正式构建需要已提交的独立 Git 源码目录。") from None
        return {"source_commit": None, "source_tree": None, "source_dirty": True}, 315532800
    dirty = bool(values[4])
    if release and dirty:
        raise SystemExit("正式构建需要干净的已提交源码；请保留当前改动后完成审阅和提交。")
    return {"source_commit": values[1], "source_tree": values[2], "source_dirty": dirty}, int(values[3])


def archive_tree(root, destination, epoch):
    date_time = time.gmtime(epoch)[:5] + (time.gmtime(epoch).tm_sec & ~1,)
    # Stored entries avoid variation between system zlib versions.
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as bundle:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                entry = zipfile.ZipInfo(root.name + "/" + path.relative_to(root).as_posix(), date_time)
                entry.create_system = 3
                entry.external_attr = (stat.S_IFREG | stat.S_IMODE(path.stat().st_mode)) << 16
                entry.compress_type = zipfile.ZIP_STORED
                bundle.writestr(entry, path.read_bytes())
    destination.chmod(0o644)


def build(output, release=False):
    source, default_epoch = source_identity(release)
    epoch_text = os.environ.get("SOURCE_DATE_EPOCH", str(default_epoch))
    if not epoch_text.isascii() or not epoch_text.isdecimal() or len(epoch_text) > 10 or int(epoch_text) > 4354819199:
        raise SystemExit("SOURCE_DATE_EPOCH 必须是 0 到 4354819199 的十进制整数。")
    epoch = max(315532800, int(epoch_text))
    names = [p.relative_to(ROOT).as_posix() for p in (ROOT / "akf").glob("*.py")]
    names += list(RUNTIME_FILES)
    for name in PUBLIC_FILES:
        if release and not (ROOT / name).is_file():
            raise SystemExit("正式构建缺少公开交付文件：" + name)
        if (ROOT / name).is_file():
            names.append(name)
    names += [name for name in OPTIONAL_FILES if (ROOT / name).is_file()]
    payload = {}
    for name in sorted(names):
        path = ROOT / name
        if any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(ROOT)) or not path.is_file():
            raise SystemExit("打包输入必须是源码目录内的普通文件：" + name)
        payload[name] = path.read_bytes()
    if release:
        listing = subprocess.run(["git", "-C", str(ROOT), "ls-tree", "-rz", source["source_commit"], "--", *payload],
                                 capture_output=True, timeout=10)
        if listing.returncode:
            raise SystemExit("无法核对正式构建的已提交文件。")
        committed = {}
        for entry in listing.stdout.split(b"\0"):
            if entry:
                metadata, name = entry.split(b"\t", 1)
                mode, kind, identity = metadata.split()
                committed[os.fsdecode(name)] = (mode, kind, identity)
        for name, data in payload.items():
            record = committed.get(name)
            if not record or record[0] not in {b"100644", b"100755"} or record[1] != b"blob":
                raise SystemExit("正式打包文件必须存在于已提交源码：" + name)
            blob = subprocess.run(["git", "-C", str(ROOT), "cat-file", "blob", record[2].decode("ascii")],
                                  capture_output=True, timeout=10)
            executable = bool((ROOT / name).stat().st_mode & 0o111)
            if blob.returncode or blob.stdout != data or executable != (record[0] == b"100755"):
                raise SystemExit("正式打包文件与已提交源码不一致：" + name)

    output = Path(output).resolve()
    package = output / ("TokemulaAgentKnowledgeWorkflow-" + __version__ + "-macos")
    marketplace = output / "agent-knowledge-workflow-marketplace"
    archive = Path(str(package) + ".zip")
    marketplace_archive = output / ("TokemulaAgentKnowledgeWorkflow-" + __version__ + "-codex-plugin.zip")
    sums = output / "SHA256SUMS"
    if any(path.exists() or path.is_symlink() for path in (package, marketplace, archive, marketplace_archive, sums)):
        raise SystemExit("输出产物已存在，请选择新的产物目录。")
    package.mkdir(parents=True)
    files = {}
    for name, data in payload.items():
        destination = package / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        mode = 0o755 if name.endswith((".sh", ".command")) else 0o644
        destination.chmod(mode)
        files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "mode": mode}
    manifest = {"name": "agent-knowledge-workflow", "version": __version__, **source,
                "private_candidate": not release, "build_epoch": epoch,
                "payload_sha256": hashlib.sha256(encoded(files)).hexdigest(), "files": files}
    (package / "PACKAGE.json").write_bytes(encoded(manifest))
    (package / "PACKAGE.json").chmod(0o644)

    plugin = marketplace / "plugins/agent-knowledge-workflow"
    shutil.copytree(package, plugin)
    # Tested Codex runtimes discover bundled hooks only from a legacy-only package.
    portable = json.loads(payload["plugin.json"])
    openai = portable["extensions"]["com.openai"]
    legacy = {**{key: portable[key] for key in ("name", "version", "description")},
              "skills": "./skills/", "hooks": openai["hooks"],
              "extensions": {"com.openai": {"onboardingSkill": openai["onboardingSkill"]}}}
    legacy_path = plugin / ".codex-plugin/plugin.json"
    legacy_path.parent.mkdir()
    legacy_data = encoded(legacy)
    legacy_path.write_bytes(legacy_data)
    legacy_path.chmod(0o644)
    (plugin / "plugin.json").unlink()
    plugin_files = dict(files)
    del plugin_files["plugin.json"]
    plugin_files[".codex-plugin/plugin.json"] = {"sha256": hashlib.sha256(legacy_data).hexdigest(), "mode": 0o644}
    (plugin / "PACKAGE.json").write_bytes(encoded({**manifest, "files": plugin_files,
        "payload_sha256": hashlib.sha256(encoded(plugin_files)).hexdigest()}))
    catalog = marketplace / ".agents/plugins/marketplace.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_bytes(encoded({"name": "agent-knowledge-workflow",
        "interface": {"displayName": "Tokemula Agent Knowledge Workflow"},
        "plugins": [{"name": "agent-knowledge-workflow", "source": {"source": "local", "path": "./plugins/agent-knowledge-workflow"},
                     "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, "category": "Productivity"}]}))
    catalog.chmod(0o644)
    for tree in (package, marketplace):
        for path in sorted(tree.rglob("*"), reverse=True):
            if path.is_dir():
                path.chmod(0o755)
            os.utime(path, (epoch, epoch))
        tree.chmod(0o755)
        os.utime(tree, (epoch, epoch))
    archive_tree(package, archive, epoch)
    archive_tree(marketplace, marketplace_archive, epoch)
    sums.write_text("".join(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name + "\n"
                            for path in sorted((archive, marketplace_archive))), encoding="utf-8")
    sums.chmod(0o644)
    result = {"package": str(package), "archive": str(archive), "marketplace": str(marketplace),
              "marketplace_archive": str(marketplace_archive), "sha256sums": str(sums)}
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--release", action="store_true", help="从干净已提交的公开源码构建正式包")
    args = parser.parse_args()
    build(args.output, release=args.release)
