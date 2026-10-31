"""Package launcher; the same entrypoint can recover an uninstalled launcher."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from akf.cli import main
from akf.install import Manager, InstallError


def install():
    arguments = sys.argv[1:]
    if arguments[:1] in (["rollback"], ["doctor"], ["uninstall"]):
        return main(arguments)
    manager = Manager()
    try:
        with manager.lock():
            result = manager.install_package(ROOT)
        if sys.stdout.isatty():
            print("工具安装完成。入口：" + result["entrypoint"])
            if result.get("transaction"):
                print("恢复事务：" + result["transaction"])
        else:
            print(json.dumps(result, ensure_ascii=False))
        return main(["setup", *arguments])
    except (InstallError, OSError, ValueError) as error:
        message = str(error) if isinstance(error, InstallError) else "安装目录不可用。请检查目录权限并重试。"
        print(json.dumps({"state": "error", "error": message}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(install())
