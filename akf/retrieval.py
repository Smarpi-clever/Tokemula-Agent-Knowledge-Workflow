"""Rebuildable local search: SQLite FTS5 plus Chinese bigrams, no network."""
from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3

from .errors import WorkflowError


INDEX_VERSION = 2
BLOCK = re.compile(r"(?m)^<!-- akf:([a-z0-9][a-z0-9_.-]{0,63}) -->\n[\s\S]*?^<!-- /akf:\1 -->")
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def bounded_result(result, budget_bytes, list_key="results"):
    """Fit complete items, including JSON metadata, into an explicit byte budget."""
    result = {**result, "budget_bytes": budget_bytes, "unit": "UTF-8 bytes", "output_bytes": 0}
    while True:
        size = encoded_size(result)
        if size > budget_bytes and result.get(list_key):
            result[list_key].pop()
            result["has_more"] = True
            continue
        if result["output_bytes"] == size:
            if size > budget_bytes:
                raise WorkflowError("input", "Output budget is too small for metadata")
            return result
        result["output_bytes"] = size


def _limits(limit, budget_bytes):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise WorkflowError("input", "Result limit must be an integer from 1 to 100")
    if isinstance(budget_bytes, bool) or not isinstance(budget_bytes, int) or not 512 <= budget_bytes <= 1_000_000:
        raise WorkflowError("input", "Budget must be 512 to 1000000 UTF-8 bytes")


def _tokens(text, *, include_unigrams=False):
    text = text.casefold()
    terms = re.findall(r"[a-z0-9_]+", text)
    for sequence in CJK.findall(text):
        terms.extend(sequence[i:i + 2] for i in range(len(sequence) - 1))
        if include_unigrams:
            terms.extend(sequence)
        elif len(sequence) == 1:
            terms.append(sequence)
    return set(terms)


def _snippet(text, query, length=360):
    folded = text.casefold()
    hits = [folded.find(term) for term in [query.casefold(), *_tokens(query)] if term in folded]
    start = max(0, min(hits, default=0) - 70)
    value = text[start:start + length].strip()
    return ("…" if start else "") + value + ("…" if start + length < len(text) else "")


class SearchIndex:
    def __init__(self, engine):
        self.engine = engine
        self.path = engine.state_dir / "search.sqlite3"
        self.index_state = "persistent"

    @contextlib.contextmanager
    def _connect(self):
        writable = os.access(self.path.parent, os.W_OK) and (not self.path.exists() or os.access(self.path, os.W_OK))
        self.index_state = "persistent" if writable else "memory_read_only_state"
        db = sqlite3.connect(self.path if writable else ":memory:", timeout=3)
        db.row_factory = sqlite3.Row
        try:
            with db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version != INDEX_VERSION:
                    for name in ("search", "passages", "files", "metadata"):
                        db.execute("DROP TABLE IF EXISTS " + name)
                    db.execute("CREATE TABLE files(path TEXT PRIMARY KEY, signature TEXT, title TEXT, sha256 TEXT, text TEXT, facts TEXT)")
                    db.execute("CREATE TABLE passages(id INTEGER PRIMARY KEY, path TEXT, payload TEXT)")
                    try:
                        db.execute("CREATE VIRTUAL TABLE search USING fts5(title, body, source)")
                    except sqlite3.OperationalError as error:
                        if "no such module: fts5" in str(error):
                            raise WorkflowError("search_unavailable", "Local SQLite requires FTS5; use a Python build with SQLite FTS5 enabled") from None
                        raise
                    db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT)")
                    db.execute("PRAGMA user_version=" + str(INDEX_VERSION))
                yield db
        finally:
            db.close()

    def _records(self):
        return self.engine.fact_records(include_history=True)

    def _delete(self, db, path):
        db.execute("DELETE FROM search WHERE rowid IN (SELECT id FROM passages WHERE path=?)", (path,))
        db.execute("DELETE FROM passages WHERE path=?", (path,))

    def _insert(self, db, passage):
        cursor = db.execute("INSERT INTO passages(path,payload) VALUES(?,?)", (
            passage["path"], json.dumps(passage, ensure_ascii=False, sort_keys=True)))
        sources = " ".join(" ".join(str(e.get(key, "")) for key in ("path", "commit", "id", "excerpt"))
                           for e in passage["sources"])
        values = [passage["title"], passage["body"], passage["path"] + " " + sources + " " + (passage.get("matter_id") or "")]
        db.execute("INSERT INTO search(rowid,title,body,source) VALUES(?,?,?,?)", (
            cursor.lastrowid, *(" ".join(sorted(_tokens(value, include_unigrams=True))) for value in values)))

    def _passage(self, path, title, body, start, end, *, fact=None, origin="human_note"):
        status = {"evidence_status": "unknown", "lifecycle": "unknown", "review_status": "unknown",
                  "review_reasons": [], "last_verified_at": None, "environment": "unknown",
                  "version_id": "unknown", "superseded_by": None, "observed_at": None,
                  "matter_id": None, "state_kind": None, "stage": None, "supersedes": [],
                  "superseded_by_claim": None, "related_current": [], "related_pending": []}
        if fact:
            status.update({k: fact.get(k, v) for k, v in status.items()})
        return {"path": path, "title": title, "body": body, "start_line": start, "end_line": end,
                "claim_id": fact.get("claim_id") if fact else None, "source_origin": origin,
                "sources": fact.get("evidence", []) if fact else [], **status}

    def _index_file(self, db, path, text, title, facts):
        self._delete(db, path)
        current = {f["claim_id"]: f for f in facts if "block" in f}
        histories = {}
        for fact in facts:
            if fact.get("lifecycle") == "superseded" and "block" not in fact:
                histories.setdefault(fact["claim_id"], []).append(fact)
        spans = []
        for match in BLOCK.finditer(text):
            spans.append((match.start(), match.end()))
            start = text.count("\n", 0, match.start()) + 1
            end = text.count("\n", 0, match.end()) + 1
            fact = current.get(match.group(1))
            if fact and match.group() == fact.get("block"):
                self._insert(db, self._passage(path, fact.get("title", title), fact["content"], start, end,
                                             fact=fact, origin="history" if fact.get("lifecycle") == "superseded" else "verified_fact"))
                for previous in histories.get(fact["claim_id"], []):
                    self._insert(db, self._passage(path, previous.get("title", title), previous["content"], start, end,
                                                 fact=previous, origin="history"))
            else:
                modified = self._passage(path, title, match.group(), start, end, origin="human_modified")
                modified.update(claim_id=match.group(1), review_status="review_required",
                                review_reasons=["Managed block differs from the saved evidence record"])
                self._insert(db, modified)
        # Exclude owned blocks from human text so historical lines cannot leak as current facts.
        lines = text.splitlines()
        owned_lines = set()
        for begin, end in spans:
            owned_lines.update(range(text.count("\n", 0, begin), text.count("\n", 0, end) + 1))
        group, group_start, heading = [], 1, title
        for number, line in enumerate(lines, 1):
            if number - 1 in owned_lines:
                if group:
                    self._insert(db, self._passage(path, heading, "\n".join(group), group_start, number - 1))
                    group = []
                continue
            if line.startswith("#") and re.match(r"^#{1,6} ", line):
                if group:
                    self._insert(db, self._passage(path, heading, "\n".join(group), group_start, number - 1))
                    group = []
                heading = line.lstrip("# ").strip()
            if not group:
                group_start = number
            group.append(line)
            if len(group) >= 24:
                self._insert(db, self._passage(path, heading, "\n".join(group), group_start, number))
                group = []
        if group and any(line.strip() for line in group):
            self._insert(db, self._passage(path, heading, "\n".join(group), group_start, len(lines)))

    def _pending(self):
        from .engine import timestamp
        passages = []
        for job in self.engine._all("job"):
            if job["state"] in {"cancelled", "rejected", "no_change"}:
                continue
            entries = job.get("pending_updates", [])
            if not entries and job.get("candidate") and job.get("error"):
                entries = [{"update": u, "error": job["error"]} for u in job["candidate"].get("updates", [])
                           if u["topic"] + ":" + u["claim_id"] not in job.get("resolved_updates", {})]
            for entry in entries:
                if entry.get("withdrawn") or entry.get("resolution") in {"rejected", "withdrawn"}:
                    continue
                update = entry["update"]
                fact = {**update, "evidence_status": "pending", "lifecycle": "candidate",
                        "review_status": "review_required", "review_reasons": [entry.get("error", "Evidence review pending")],
                        "environment": update.get("environment", self.engine.config.get("environment", "unknown"))}
                passage = self._passage(update["topic"], update["title"], update["content"], None, None,
                                        fact=fact, origin="pending")
                passage["job_id"] = job["id"]
                passage["item_id"] = entry.get("id", update["topic"] + ":" + update["claim_id"])
                passages.append(passage)
        groups = {}
        for passage in passages:
            groups.setdefault((passage["path"], passage["claim_id"], passage["environment"]), []).append(passage)
        for group in groups.values():
            dated = [passage for passage in group if passage.get("observed_at")]
            if len(dated) < 2:
                continue
            dated.sort(key=lambda passage: timestamp(passage["observed_at"]), reverse=True)
            latest = dated[0]
            older = [passage for passage in dated[1:] if timestamp(passage["observed_at"]) < timestamp(latest["observed_at"]) or passage["body"] == latest["body"]]
            latest["related_pending"] = [{"job": passage["job_id"], "item": passage["item_id"],
                                           "observed_at": passage["observed_at"]} for passage in older]
            for passage in older:
                passage.update(lifecycle="superseded", superseded_by=latest["job_id"],
                               superseded_by_claim=latest["item_id"])
        return passages

    def _refresh(self, db):
        from .engine import digest
        facts = {}
        for fact in self._records():
            facts.setdefault(fact["topic"], []).append(fact)
        cached = {row["path"]: row for row in db.execute("SELECT path,signature,facts FROM files")}
        present = set()
        root = self.engine._path(self.engine.knowledge_root, "topics")
        for listed in sorted(root.rglob("*.md")):
            relative = listed.relative_to(self.engine.knowledge_root).as_posix()
            try:
                path = self.engine._path(self.engine.knowledge_root, relative, note=True)
            except WorkflowError:
                continue
            if not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            info = path.stat()
            signature = json.dumps([info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino])
            state = json.dumps(facts.get(relative, []), ensure_ascii=False, sort_keys=True)
            present.add(relative)
            old = cached.get(relative)
            if old and old["signature"] == signature and old["facts"] == state:
                continue
            if old and old["signature"] == signature:
                row = db.execute("SELECT text,title,sha256 FROM files WHERE path=?", (relative,)).fetchone()
                text, title, sha = row["text"], row["title"], row["sha256"]
            else:
                data = path.read_bytes()
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    present.discard(relative)
                    continue
                title = next((line[2:] for line in text.splitlines() if line.startswith("# ")), path.stem)[:200]
                sha = digest(data)
            db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?)", (relative, signature, title, sha, text, state))
            self._index_file(db, relative, text, title, facts.get(relative, []))
        for missing in cached.keys() - present:
            self._delete(db, missing)
            db.execute("DELETE FROM files WHERE path=?", (missing,))
        pending = self._pending()
        serialized = json.dumps(pending, ensure_ascii=False, sort_keys=True)
        previous = db.execute("SELECT value FROM metadata WHERE key='pending'").fetchone()
        if not previous or previous[0] != serialized:
            db.execute("DELETE FROM search WHERE rowid IN (SELECT id FROM passages WHERE json_extract(payload,'$.source_origin')='pending')")
            db.execute("DELETE FROM passages WHERE json_extract(payload,'$.source_origin')='pending'")
            for passage in pending:
                self._insert(db, passage)
            db.execute("INSERT OR REPLACE INTO metadata VALUES('pending',?)", (serialized,))

    def rebuild(self):
        with self._connect() as db:
            db.execute("DELETE FROM search")
            db.execute("DELETE FROM passages")
            db.execute("DELETE FROM files")
            db.execute("DELETE FROM metadata")
            self._refresh(db)
            return {"state": "rebuilt", "index": str(self.path) if self.index_state == "persistent" else None,
                    "index_state": self.index_state,
                    "files": db.execute("SELECT count(*) FROM files").fetchone()[0],
                    "passages": db.execute("SELECT count(*) FROM passages").fetchone()[0],
                    "strategy": "FTS5 unicode61 with Chinese bigrams and single-character lookup"}

    def topics(self):
        with self._connect() as db:
            self._refresh(db)
            topics = []
            for row in db.execute("SELECT path,title,sha256,facts FROM files ORDER BY path"):
                facts = [fact for fact in json.loads(row["facts"]) if fact.get("lifecycle") != "superseded"]
                observed = [fact.get("last_verified_at") or fact.get("observed_at") for fact in facts]
                sources = [{k: evidence[k] for k in ("kind", "path", "sha256", "commit", "id", "observed_at") if k in evidence}
                           for fact in facts for evidence in fact.get("evidence", [])]
                topics.append({"path": row["path"], "title": row["title"], "sha256": row["sha256"],
                               "verified_at": max((value for value in observed if value), default=None), "sources": sources})
            return topics

    def search(self, query, limit=5, budget_bytes=6000, history=False):
        from .engine import timestamp
        _limits(limit, budget_bytes)
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise WorkflowError("input", "Query must contain 1 to 500 characters")
        terms = _tokens(query)
        if not isinstance(history, bool):
            raise WorkflowError("input", "History must be a boolean")
        matches = []
        with self._connect() as db:
            self._refresh(db)
            if terms:
                expression = " OR ".join('"' + term + '"' for term in sorted(terms))
                rows = db.execute("SELECT p.payload FROM search JOIN passages p ON p.id=search.rowid WHERE search MATCH ? ORDER BY bm25(search,4,1,3)", (expression,))
                for row in rows:
                    passage = json.loads(row[0])
                    if not history and passage["lifecycle"] == "superseded":
                        continue
                    title_terms, body_terms = _tokens(passage["title"], include_unigrams=True), _tokens(passage["body"], include_unigrams=True)
                    source_terms = _tokens(passage["path"] + " " + json.dumps(passage["sources"], ensure_ascii=False), include_unigrams=True)
                    coverage = len(terms & (title_terms | body_terms | source_terms)) / len(terms)
                    score = coverage * 10 + len(terms & title_terms) * 2 + len(terms & source_terms)
                    if query.casefold() in (passage["title"] + " " + passage["body"]).casefold():
                        score += 5
                    # Current facts win ties while matching history remains accessible explicitly.
                    priority = {"current": 3, "unknown": 2, "candidate": 1, "superseded": 0}.get(passage["lifecycle"], 0)
                    matches.append((score, priority, passage))
        matches.sort(key=lambda item: (-item[0], -item[1],
                                      -timestamp(item[2]["observed_at"]).timestamp() if item[2].get("observed_at") else float("inf"),
                                      item[2]["path"], item[2]["start_line"] or 0))
        results = []
        for score, priority, passage in matches[:limit]:
            value = {k: v for k, v in passage.items() if k != "body"}
            value["sources"] = [{k: e[k] for k in ("kind", "path", "sha256", "commit", "id", "observed_at", "exit_code") if k in e}
                                for e in value["sources"]]
            value["snippet"] = _snippet(passage["body"], query)
            if not terms & _tokens(passage["title"] + " " + passage["body"], include_unigrams=True):
                for source in passage["sources"]:
                    source_text = " ".join(str(source.get(key, "")) for key in ("path", "commit", "id", "excerpt"))
                    if terms & _tokens(source_text, include_unigrams=True) and source.get("excerpt"):
                        value["snippet"] = _snippet(source["excerpt"], query)
                        value["snippet_source"] = {k: source[k] for k in ("kind", "path", "commit", "id") if k in source}
                        break
            value["read"] = ({"topic": passage["path"], "start_line": passage["start_line"], "end_line": passage["end_line"]}
                             if passage["start_line"] else {"job": passage.get("job_id"), "item": passage.get("item_id")})
            results.append(value)
        return bounded_result({"query": query, "results": results, "matched_count": len(matches),
                               "index_state": self.index_state,
                               "has_more": len(matches) > limit}, budget_bytes)

    def read(self, topic, start_line=1, end_line=None, budget_bytes=6000):
        _limits(1, budget_bytes)
        if isinstance(start_line, bool) or not isinstance(start_line, int) or start_line < 1:
            raise WorkflowError("input", "Start line must be a positive integer")
        if end_line is not None and (isinstance(end_line, bool) or not isinstance(end_line, int) or end_line < start_line):
            raise WorkflowError("input", "End line must be at least the start line")
        self.engine._path(self.engine.knowledge_root, topic, note=True)
        with self._connect() as db:
            self._refresh(db)
            row = db.execute("SELECT text,sha256 FROM files WHERE path=?", (topic,)).fetchone()
            if not row:
                raise WorkflowError("path", "Knowledge note is unavailable")
            lines = row["text"].splitlines()
            end = min(end_line or len(lines), len(lines))
            selected = lines[start_line - 1:end]
            result = {"path": topic, "sha256": row["sha256"], "start_line": start_line,
                      "index_state": self.index_state,
                      "end_line": start_line - 1, "total_lines": len(lines), "lines": [],
                      "has_more": end < len(lines), "next_line": None}
            for line in selected:
                result["lines"].append(line)
                result["end_line"] += 1
                result["has_more"] = result["end_line"] < len(lines)
                result["next_line"] = result["end_line"] + 1 if result["has_more"] else None
                candidate = bounded_result(dict(result, lines=list(result["lines"])), budget_bytes, list_key="lines")
                if len(candidate["lines"]) < len(result["lines"]):
                    result["lines"].pop()
                    result["end_line"] -= 1
                    result["has_more"] = True
                    result["next_line"] = result["end_line"] + 1
                    break
            if selected and not result["lines"]:
                raise WorkflowError("input", "First requested line exceeds budget; increase --budget-bytes")
            return bounded_result(result, budget_bytes, list_key="lines")
