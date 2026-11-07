"""Compare fixed synthetic queries against v0.2.2 and the current local runtime."""
import argparse
import datetime as dt
import io
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "2ac1ee2411e8345ec6043bab907a4b6a5b25af5f"


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")


def score(paths, relevant, k=5):
    ranked = paths[:k]
    if not relevant:
        return {"recall_at_5": None, "mrr_at_5": None, "empty_correct": not ranked}
    hits = set(ranked) & set(relevant)
    rank = next((i for i, path in enumerate(ranked, 1) if path in relevant), None)
    return {"recall_at_5": len(hits) / len(set(relevant)), "mrr_at_5": 1 / rank if rank else 0}


def seed(engine, corpus):
    """Use each release's ordinary evidence verifier; no hand-written fact state."""
    from akf.engine import digest
    for topic in corpus["manual_topics"]:
        path = engine.knowledge_root / topic["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(topic["text"], encoding="utf-8")
    counter = 0
    for spec in corpus["facts"]:
        source = engine.project_root / spec["source_path"]
        source.parent.mkdir(parents=True, exist_ok=True)
        for version in spec["versions"]:
            counter += 1
            turn = "seed-" + str(counter)
            engine.start_turn("evaluation", turn, "a" * 64)
            source.write_text(version["source"], encoding="utf-8")
            engine.record_receipt("evaluation", turn, turn, "Bash",
                                  {"cmd": "cat " + spec["source_path"]}, {"exit_code": 0})
            job = engine.prepare_maintenance("evaluation", turn)
            note = engine.knowledge_root / spec["topic"]
            update = {"topic": spec["topic"], "title": spec["title"], "claim_id": spec["claim_id"],
                      "claim_type": "file_fact", "content": version["content"],
                      "observed_at": (dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc) +
                                      dt.timedelta(minutes=counter)).isoformat(),
                      "expected_sha256": digest(note.read_bytes()) if note.exists() else None,
                      "evidence": [{"kind": "file", "path": spec["source_path"],
                                    "sha256": digest(source.read_bytes()), "excerpt": version["source"].strip()}]}
            result = engine.submit(job["id"], {"status": "completed", "updates": [update]})
            if result["state"] != "local_saved":
                raise RuntimeError("Synthetic fact could not be saved: " + json.dumps(result))
        if "changed_source_after_verification" in spec:
            source.write_text(spec["changed_source_after_verification"], encoding="utf-8")


def worker(runtime, fixture, candidate, corpus_path, queries_path):
    sys.path.insert(0, str(runtime))
    from akf.engine import Engine
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    suite = json.loads(queries_path.read_text(encoding="utf-8"))
    project, knowledge = fixture / "project", fixture / "knowledge"
    project.mkdir(parents=True)
    knowledge.mkdir()
    config = fixture / "config.json"
    config.write_text(json.dumps({"knowledge_id": "fixed-evaluation", "project_root": str(project),
                                 "knowledge_root": str(knowledge), "state_dir": str(fixture / "state"),
                                 "environment": "synthetic", "project_snapshot": False, "enabled": True,
                                 "git": {"auto_commit": False, "auto_push": False}}), encoding="utf-8")
    engine = Engine(config)
    try:
        seed(engine, corpus)
        if candidate:
            from akf.retrieval import SearchIndex
            index = SearchIndex(engine)

            def query(item):
                return index.search(item["query"], limit=suite["k"], budget_bytes=suite["budget_bytes"],
                                    history=item.get("history", False))

            def results(value):
                return value["results"]
        else:
            def query(item):
                return engine.context(item["query"])

            def results(value):
                return value["topics"]
        query(suite["queries"][0])
        rows = []
        before_edit_empty = None
        for item in suite["queries"]:
            if item.get("after_edit"):
                before_edit_empty = not results(query(item))
                edit = corpus["human_edit"]
                path = knowledge / edit["path"]
                path.write_text(path.read_text(encoding="utf-8") + edit["append"], encoding="utf-8")
            started = time.perf_counter_ns()
            value = query(item)
            first_ms = (time.perf_counter_ns() - started) / 1_000_000
            timings = []
            for _ in range(5):
                started = time.perf_counter_ns()
                query(item)
                timings.append((time.perf_counter_ns() - started) / 1_000_000)
            native = results(value)
            row = {**item, **score([entry["path"] for entry in native], item["relevant"], suite["k"]),
                   "paths_at_5": [entry["path"] for entry in native[:suite["k"]]],
                   "context_bytes": len(encoded(value)), "top_5_results_bytes": len(encoded(native[:suite["k"]])),
                   "first_query_ms": first_ms, "warm_median_ms": statistics.median(timings),
                   "warm_samples_ms": timings, "native_result": value}
            rows.append(row)
        by_id = {row["id"]: row for row in rows}
        current = results(by_id["current-fact"]["native_result"])[:suite["k"]]
        historical = results(by_id["history-fact"]["native_result"])[:suite["k"]]
        review = results(by_id["review-required"]["native_result"])[:suite["k"]]
        current_facts = [entry for entry in current if entry["path"] == "topics/30_limits.md"]
        historical_facts = [entry for entry in historical if entry["path"] == "topics/30_limits.md"]
        review_facts = [entry for entry in review if entry["path"] == "topics/31_retry.md"]
        correctness = {
            "no_match_empty": by_id["no-match"]["empty_correct"],
            "human_edit_prequery_empty": before_edit_empty,
            "human_edit_visible": by_id["human-edit"]["recall_at_5"] == 1,
            "fact_state_labels_available": any("lifecycle" in entry for entry in current_facts),
            "history_version_labelled": any(entry.get("lifecycle") == "superseded" for entry in historical_facts),
            "changed_source_labelled_review_required": any(entry.get("review_status") == "review_required" for entry in review_facts),
            "default_old_content_misuse": (any(
                ("并发上限为 8" in entry.get("snippet", "") or "concurrency limit 8" in entry.get("snippet", ""))
                and entry.get("lifecycle") != "superseded" for entry in current_facts) if candidate else None),
            "current_version_labelled": any(entry.get("lifecycle") == "current" and
                                            "16" in entry.get("snippet", "") for entry in current_facts),
        }
        # Actual CLI timings include interpreter startup and a warm on-disk index.
        cold = []
        sample = suite["queries"][0]
        command = [sys.executable, "-B", "-m", "akf", "--config", str(config)]
        command += (["search", "--query", sample["query"], "--limit", str(suite["k"]),
                     "--budget-bytes", str(suite["budget_bytes"])] if candidate else
                    ["context", "--task", sample["query"]])
        env = {**os.environ, "PYTHONPATH": str(runtime), "PYTHONDONTWRITEBYTECODE": "1",
               "AKF_HOME": str(fixture / "support"), "AKF_INSTALL_ROOT": str(fixture / "tool"),
               "AKF_BIN_DIR": str(fixture / "bin"), "CODEX_HOME": str(fixture / "codex"),
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        for _ in range(3):
            started = time.perf_counter_ns()
            proc = subprocess.run(command, cwd=fixture, env=env, capture_output=True, text=True, timeout=20)
            if proc.returncode:
                raise RuntimeError("CLI evaluation failed: " + proc.stdout + proc.stderr)
            json.loads(proc.stdout)
            cold.append((time.perf_counter_ns() - started) / 1_000_000)
        scored = [row for row in rows if row["relevant"]]
        summary = {"recall_at_5": statistics.mean(row["recall_at_5"] for row in scored),
                   "mrr_at_5": statistics.mean(row["mrr_at_5"] for row in scored),
                   "mean_context_bytes": statistics.mean(row["context_bytes"] for row in rows),
                   "mean_top_5_results_bytes": statistics.mean(row["top_5_results_bytes"] for row in rows),
                   "median_warm_query_ms": statistics.median(row["warm_median_ms"] for row in rows),
                   "cli_cold_start_median_ms": statistics.median(cold), "cli_cold_samples_ms": cold,
                   "scored_queries": len(scored), "queries": len(rows)}
        return {"summary": summary, "correctness": correctness, "queries": rows,
                "failures": [row["id"] for row in rows if row.get("recall_at_5") == 0 or row.get("empty_correct") is False]}
    finally:
        engine.db.close()


def extract_baseline(destination):
    marker = destination / ".evaluation-baseline"
    if destination.exists():
        if marker.is_file() and marker.read_text().strip() == BASELINE:
            return
        raise SystemExit("基线源码目录已存在且缺少匹配的提取记录：" + str(destination))
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", BASELINE],
                             capture_output=True, check=True).stdout
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        for member in bundle.getmembers():
            if member.isfile():
                path = destination / member.name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(bundle.extractfile(member).read())
                path.chmod(member.mode)
    marker.write_text(BASELINE + "\n", encoding="utf-8")


def report_markdown(report):
    lines = ["# AKF v0.3.0 固定检索评测", "", "基线：`" + BASELINE + "`（v0.2.2）。",
             "语料：15 个合成主题，12 条固定查询；11 条有相关主题查询计入 Recall/MRR。", "",
             "| 指标 | v0.2.2 context | v0.3 search |", "|---|---:|---:|"]
    metrics = [("Recall@5", "recall_at_5"), ("MRR@5", "mrr_at_5"),
               ("平均原生 JSON UTF-8 bytes", "mean_context_bytes"),
               ("平均前5结果 JSON UTF-8 bytes", "mean_top_5_results_bytes"),
               ("进程内预热查询中位数 ms", "median_warm_query_ms"),
               ("含 CLI 冷启动中位数 ms", "cli_cold_start_median_ms")]
    for label, key in metrics:
        lines.append(f"| {label} | {report['baseline']['summary'][key]:.4f} | {report['candidate']['summary'][key]:.4f} |")
    lines += ["", "## 独立正确性", "", "| 检查 | v0.2.2 | v0.3 |", "|---|---|---|"]
    for key, value in report["candidate"]["correctness"].items():
        def label(item):
            return "未知（无正文/状态输出）" if item is None else "true" if item else "false"
        lines.append(f"| {key} | {label(report['baseline']['correctness'][key])} | {label(value)} |")
    lines += ["", "## 逐查询与失败样例", "", "| 查询 | 基线排名前5命中 | v0.3排名前5命中 |", "|---|---:|---:|"]
    for left, right in zip(report["baseline"]["queries"], report["candidate"]["queries"]):
        lines.append(f"| {left['id']} | {left.get('recall_at_5')} | {right.get('recall_at_5')} |")
    lines += ["", "基线失败：" + ", ".join(report["baseline"]["failures"]),
              "v0.3 失败：" + (", ".join(report["candidate"]["failures"]) or "无"), "",
              "评分口径见仓库 `evaluation/README.md`。完整结果及失败输入保存在 `report.json`。",
              "基线原生 context 返回全部主题元数据，前5用于共同相关性评分；v0.3 search 使用 5 条与 6000 bytes 上限。",
              "旧内容误用基线记未知；状态标签缺失单独记录。Token 数未估算。耗时包含本机负载。", ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".local/v0.3.0/evaluation")
    parser.add_argument("--worker", choices=("baseline", "candidate"), help=argparse.SUPPRESS)
    parser.add_argument("--runtime", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--fixture", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    corpus, queries = ROOT / "evaluation/corpus.json", ROOT / "evaluation/queries.json"
    if args.worker:
        print(json.dumps(worker(args.runtime, args.fixture, args.worker == "candidate", corpus, queries), ensure_ascii=False))
        return 0
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    baseline = output.parent / "baseline-source"
    extract_baseline(baseline)
    results = {}
    with tempfile.TemporaryDirectory(prefix="fixtures-", dir=output) as temporary:
        for name, runtime in (("baseline", baseline), ("candidate", ROOT)):
            command = [sys.executable, "-B", str(Path(__file__).resolve()), "--worker", name,
                       "--runtime", str(runtime), "--fixture", str(Path(temporary) / name)]
            proc = subprocess.run(command, capture_output=True, text=True, timeout=120,
                                  env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            if proc.returncode:
                raise SystemExit(proc.stderr + proc.stdout)
            results[name] = json.loads(proc.stdout)
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    report = {"baseline_commit": BASELINE, "candidate_source_head": commit,
              "candidate_source_note": "Current working tree; final delivery commit is recorded by the release owner.",
              "corpus": "evaluation/corpus.json", "queries": "evaluation/queries.json", **results}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "report.md").write_text(report_markdown(report), encoding="utf-8")
    print(json.dumps({"report": str(output / "report.md"),
                      "baseline": results["baseline"]["summary"], "candidate": results["candidate"]["summary"],
                      "candidate_correctness": results["candidate"]["correctness"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
