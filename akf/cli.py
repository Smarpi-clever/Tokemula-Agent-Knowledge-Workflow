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
    prepare = sub.add_parser("prepare", help="在当前任务最终答复前准备知识维护证据")
    prepare.add_argument("--session", required=True)
    prepare.add_argument("--turn", required=True)
    context = sub.add_parser("context")
    context.add_argument("--task", default="")
    for entry in (context, sub.add_parser("search", help="本地检索相关知识片段")):
        if entry is not context:
            entry.add_argument("--query", required=True)
        entry.add_argument("--limit", type=int, default=5)
        entry.add_argument("--budget-bytes", type=int, default=6000, help="完整 JSON 输出的 UTF-8 字节预算")
        entry.add_argument("--history", action="store_true", help="包含已替代事实")
    read = sub.add_parser("read", help="按行读取知识及来源")
    read.add_argument("--path", required=True)
    read.add_argument("--start-line", type=int, default=1)
    read.add_argument("--end-line", type=int)
    read.add_argument("--budget-bytes", type=int, default=6000)
    index = sub.add_parser("index", help="更新或重建本地派生索引")
    index.add_argument("--rebuild", action="store_true")
    status = sub.add_parser("status")
    status.add_argument("--job")
    sub.add_parser("pending")
    report = sub.add_parser("select-report", help="显式选定一个私有发布报告作为内容证据")
    report.add_argument("--path", required=True, help="项目 .local/ 下的具体报告")
    for command in ("review", "explain"):
        entry = sub.add_parser(command, help="逐项审阅待处理候选" if command == "review" else "解释候选及证据缺口")
        entry.add_argument("--job", required=True)
        entry.add_argument("--item", help="topics/name.md:claim-id")
        if command == "review":
            entry.add_argument("--action", choices=("show", "revalidate", "split", "reanchor", "link-history", "reject", "withdraw", "confirm"), default="show")
            entry.add_argument("--reason", help="本次审阅、保留或撤回的具体原因")
            entry.add_argument("--fact", help="关联已核验历史事实 topics/name.md:claim-id")
            entry.add_argument("--version", help="关联具体历史 version_id；默认当前版本")
            entry.add_argument("--expected-sha256", help="显式重新审阅的当前笔记 SHA-256，用于 reanchor")
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
    for name in ("context", "search", "read", "index", "pending", "status", "review", "explain", "select-report"):
        sub.choices[name].add_argument("--json", action="store_true", default=argparse.SUPPRESS)
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
                elif mapping and (mapping["paused"] or mapping["disabled"] or not manager.registry().get("installed") or mapping.get("archived") and args.command == "hook") and args.command in {"hook", "prepare", "submit", "resume", "undo", "review"}:
                    result = {} if args.command == "hook" else {"state": "paused"}
                else:
                    from .engine import Engine
                    engine = Engine(args.config)
                    try:
                        result = business(args, engine)
                    finally:
                        engine.db.close()
        if not args.json and sys.stdout.isatty() and args.command in {"search", "read", "context", "pending", "review", "explain"}:
            render(result)
        elif not args.json and sys.stdout.isatty() and (args.command in setup_cli.COMMANDS - {"managed-hook"} or "states" in result):
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
            sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
            if args.command not in {"search", "read", "context"}:
                sys.stdout.write("\n")
        return 0
    except (WorkflowError, InstallError, ValueError, OSError) as error:
        message = str(error) if isinstance(error, (WorkflowError, InstallError)) else "输入或本地资源不可用，请运行 akf doctor。"
        result = {"systemMessage": "Knowledge workflow: " + message} if args.command in {"hook", "managed-hook"} else {"state": "error", "error": message}
        print(json.dumps(result, ensure_ascii=False))
        return 1
    except (KeyboardInterrupt, EOFError):
        print(json.dumps({"state": "cancelled", "next_step": "已取消。重新运行安装入口可继续；自动权限保持原有选择。"}, ensure_ascii=False))
        return 130


def render(result):
    """Keep terminal summaries readable; --json retains every machine field."""
    if "results" in result:
        for item in result["results"]:
            print(f"{item['path']}:{item.get('start_line') or '?'}  {item['title']}")
            print("  " + " / ".join(str(item.get(key, "unknown")) for key in
                                    ("source_origin", "evidence_status", "lifecycle", "review_status")))
            print("  " + item.get("snippet", ""))
            for source in item.get("sources", []):
                print("  来源：" + source.get("path", source.get("id", "unknown")))
        if not result["results"]:
            print("无相关结果。" if not result.get("has_more") else "预算内无完整结果；请提高 --budget-bytes。")
    elif "lines" in result:
        print(f"{result['path']}  SHA-256 {result['sha256']}")
        for number, line in enumerate(result["lines"], result["start_line"]):
            print(f"{number}: {line}")
        if result.get("next_line"):
            print(f"继续：--start-line {result['next_line']}")
    elif "items" in result:
        print(f"任务 {result['job_id']}：{result['state']}，待处理 {result['pending_count']} 项")
        import shlex
        for item in result["items"]:
            print(f"\n{item['item_id']}  {item['resolution']}  {item['proposed_change']}")
            print(item["candidate"]["content"])
            target = item.get("target_fact")
            if target:
                print("目标事实：" + target["content"])
                print("  " + " / ".join(str(target.get(key, "unknown")) for key in
                                        ("evidence_status", "lifecycle", "review_status")))
            else:
                print("目标事实：新增")
            print("来源：" + json.dumps(item["sources"], ensure_ascii=False))
            print("证据缺口：" + str(item.get("evidence_gap") or "无"))
            print("核验要求：" + item["verification_required"])
            if item.get("conflict_reason"):
                print("冲突：" + item["conflict_reason"])
            if item.get("manual_confirmation"):
                print("人工确认：" + item["manual_confirmation"]["at"])
            if item.get("reason"):
                print("处理原因：" + item["reason"])
            if item.get("linked_fact"):
                print("关联历史：" + json.dumps(item["linked_fact"], ensure_ascii=False))
            for action in item["actions"]:
                print("处理：" + shlex.join([*item["review_command"], "--action", action]) +
                      (" < update.json" if action == "revalidate" else
                       " --reason '已核对部分范围' < split.json" if action == "split" else
                       " --expected-sha256 CURRENT_SHA --reason '已核对当前笔记'" if action == "reanchor" else
                       " --fact topics/name.md:claim-id --reason '对应已核验历史'" if action == "link-history" else ""))
    else:
        for job in result.get("jobs", result.get("pending_jobs", [])):
            print(f"{job['id']}  {job['state']}  待处理 {job['pending_count']} 项")
            print("  " + json.dumps(job.get("issues", {}), ensure_ascii=False))
        for topic in result.get("topics", []):
            print(topic["path"] + "  " + topic["title"])
    if result.get("has_more"):
        print("结果已截短；可提高 --limit 或 --budget-bytes。")


def business(args, engine):
    if args.command in {"hook", "submit"} or args.command == "review" and args.action in {"revalidate", "split"}:
        raw = sys.stdin.buffer.read(1_048_577)
        if len(raw) > 1_048_576:
            raise WorkflowError("input", "Input exceeds the bounded event size")
        payload = json.loads(raw)
        if args.command == "hook":
            from .hooks import handle_event
            result = handle_event(payload, engine)
        else:
            if args.command == "review":
                from .review import review
                result = review(engine, args.job, args.action, args.item, payload, reason=args.reason)
            else:
                result = engine.submit(args.job, payload)
    elif args.command == "prepare":
        job = engine.prepare_maintenance(args.session, args.turn)
        if job is None:
            result = {"state": "not_prepared"}
        else:
            from .hooks import maintenance_instructions
            result = {"job": job, "instructions": maintenance_instructions(job)}
    elif args.command == "select-report":
        from .reports import select_report
        result = select_report(engine, args.path)
    elif args.command == "context":
        result = engine.context(args.task, limit=args.limit, budget_bytes=args.budget_bytes, history=args.history)
    elif args.command in {"search", "read", "index"}:
        from .retrieval import SearchIndex
        index = SearchIndex(engine)
        if args.command == "search":
            result = index.search(args.query, limit=args.limit, budget_bytes=args.budget_bytes, history=args.history)
        elif args.command == "read":
            result = index.read(args.path, args.start_line, args.end_line, args.budget_bytes)
        elif args.rebuild:
            result = index.rebuild()
        else:
            result = {"topics": index.topics()}
    elif args.command == "status":
        result = engine.status(args.job)
    elif args.command == "pending":
        result = engine.status()
        result["jobs"] = [job for job in result["jobs"] if not engine.job_complete(engine._get("job", job["id"]))]
    elif args.command == "explain":
        result = engine.explain(args.job, args.item)
    elif args.command == "review":
        from .review import review
        result = review(engine, args.job, args.action, args.item, reason=args.reason,
                        linked_fact=args.fact, linked_version=args.version, expected_sha256=args.expected_sha256)
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
