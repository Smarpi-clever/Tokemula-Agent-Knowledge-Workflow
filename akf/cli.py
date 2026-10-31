"""One local entrypoint; all dynamic candidate data is treated as data."""
import argparse
import json
import sys
import contextlib

from .errors import WorkflowError
from .install import InstallError, Manager


def main(argv=None):
    parser = argparse.ArgumentParser(prog="akf")
    parser.add_argument("--config")
    parser.add_argument("--mapping", help="明确选择项目映射 ID")
    parser.add_argument("--json", action="store_true", help="保留 JSON 输出")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook")
    context = sub.add_parser("context")
    context.add_argument("--task", default="")
    status = sub.add_parser("status")
    status.add_argument("--job")
    sub.add_parser("pending")
    obsidian = sub.add_parser("obsidian-check")
    obsidian.add_argument("--topic", required=True)
    search = sub.add_parser("obsidian-search")
    search.add_argument("--query", required=True)
    for command in ("submit", "resume", "undo"):
        entry = sub.add_parser(command)
        entry.add_argument("--job", required=True)
    for command in ("pause", "disable", "enable"):
        sub.add_parser(command)
    from . import setup_cli
    setup_cli.add_commands(sub)
    args = parser.parse_args(argv)
    try:
        if args.command in setup_cli.COMMANDS or (not args.config and args.command in {"status", "pause", "disable", "enable"} and not getattr(args, "job", None)):
            result = setup_cli.run(args)
        else:
            manager = Manager()
            mapping = None
            if not args.config:
                mapping = manager.select(identity=args.mapping)
                if not mapping:
                    raise InstallError("当前项目尚未配置。请运行 akf setup 或指定 --config。")
                args.config = mapping["config_path"]
            else:
                from pathlib import Path
                config_path = Path(args.config).resolve()
                if config_path.parent.name == "configs" and (config_path.parent.parent / "installation.json").is_file():
                    owner = Manager(config_path.parent.parent)
                    if any(Path(m["config_path"]).resolve() == config_path for m in owner.registry()["mappings"].values()):
                        manager = owner
                mapping = next((m for m in manager.registry()["mappings"].values()
                                if Path(m["config_path"]).resolve() == Path(args.config).resolve()), None)
            with manager.lock() if mapping else contextlib.nullcontext():
                if mapping:
                    mapping = manager.registry()["mappings"][mapping["id"]]
                    manager.config(mapping)
                if mapping and args.command in {"pause", "disable", "enable"}:
                    result = manager.control(mapping["id"], args.command)
                elif mapping and (mapping["paused"] or mapping["disabled"] or not manager.registry().get("installed") or mapping.get("archived") and args.command == "hook") and args.command in {"hook", "submit", "resume", "undo"}:
                    result = {} if args.command == "hook" else {"state": "paused"}
                else:
                    from .engine import Engine
                    engine = Engine(args.config)
                    try:
                        result = business(args, engine)
                    finally:
                        engine.db.close()
        if not args.json and sys.stdout.isatty() and (args.command in setup_cli.COMMANDS - {"managed-hook"} or "states" in result):
            if "states" in result:
                states = result["states"]
                print("工具安装：" + ("已完成" if states["Installed"] else "待安装或修复"))
                print("项目绑定：" + ("已配置" if states["Configured"] else "待设置"))
                print("Hook 信任：" + states["Trusted"])
                print("自动维护：" + ("已启用" if states["Enabled"] else "未启用或待验证"))
                print("本地自动提交：" + ("开启" if states["Sync Enabled"]["commit"] else "关闭"))
                print("远端自动推送：" + ("开启" if states["Sync Enabled"]["push"] else "关闭"))
                for message in result.get("errors", []):
                    print(message)
                for message in result.get("environment", {}).get("next_steps", []):
                    print(message)
            else:
                print("状态：" + {"no_op": "现有设置一致", "applied": "已应用", "preview": "预览完成", "rolled_back": "已回滚"}.get(result.get("state"), result.get("state", "待确认")))
                if result.get("transaction"):
                    print("恢复事务：" + result["transaction"])
            if result.get("next_step"):
                print(result["next_step"])
        else:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (WorkflowError, InstallError, ValueError, OSError) as error:
        message = str(error) if isinstance(error, (WorkflowError, InstallError)) else "输入或本地资源不可用，请运行 akf doctor。"
        result = {"systemMessage": "Knowledge workflow: " + message} if args.command in {"hook", "managed-hook"} else {"state": "error", "error": message}
        print(json.dumps(result, ensure_ascii=False))
        return 1
    except (KeyboardInterrupt, EOFError):
        print(json.dumps({"state": "cancelled", "next_step": "已取消。重新运行安装入口可继续；自动权限保持原有选择。"}, ensure_ascii=False))
        return 130


def business(args, engine):
    if args.command in {"hook", "submit"}:
        raw = sys.stdin.buffer.read(1_048_577)
        if len(raw) > 1_048_576:
            raise WorkflowError("input", "Input exceeds the bounded event size")
        payload = json.loads(raw)
        if args.command == "hook":
            from .hooks import handle_event
            result = handle_event(payload, engine)
        else:
            result = engine.submit(args.job, payload)
    elif args.command == "context":
        result = engine.context(args.task)
    elif args.command == "status":
        result = engine.status(args.job)
    elif args.command == "pending":
        result = engine.status()
    elif args.command in {"obsidian-check", "obsidian-search"}:
        from .obsidian import check_note, search_topics
        result = check_note(engine, args.topic) if args.command == "obsidian-check" else search_topics(engine, args.query)
    elif args.command == "resume":
        result = engine.resume(args.job)
    elif args.command == "undo":
        result = engine.undo(args.job)
    else:
        result = engine.control(args.command)
    return result
