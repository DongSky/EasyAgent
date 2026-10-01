"""Long-term memory for EasyAgent rewrite (SPEC §10).

``append_learning`` appends one JSON line ``{ts, text, tags}`` to ``learnings.jsonl``.
``search`` does CJK bigram tokenization + TF scoring (lexical only, no vectors),
following the ``terms()`` idea in the original repo's ``knowledge.py``.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _terms(text: str) -> list[str]:
    """Tokenize like the original knowledge.terms(): word tokens plus CJK bigrams."""
    lowered = text.casefold()
    words = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]", lowered)
    bigrams = [
        lowered[i : i + 2]
        for i in range(len(lowered) - 1)
        if "\u3400" <= lowered[i] <= "\u9fff" and "\u3400" <= lowered[i + 1] <= "\u9fff"
    ]
    return words + bigrams


def _count(term: str, text: str) -> int:
    """Count (overlapping) occurrences of term in casefolded text: term frequency."""
    count, start = 0, 0
    while True:
        i = text.find(term, start)
        if i < 0:
            return count
        count += 1
        start = i + 1


def _score(text: str, query_terms: list[str]) -> float:
    lowered = text.casefold()
    return sum(_count(term, lowered) for term in query_terms)


def _sentences(text: str) -> list[str]:
    """Split text into sentences (CJK + ASCII), stripped, order-preserving."""
    parts = re.split(r"[。！？!?…\n]+", text)
    return [p.strip(" \t\r\"'“”‘’") for p in parts if p.strip(" \t\r\"'“”‘’")]


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class Memory:
    """Append-only learning log with lexical TF search."""

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(_repo_root(), "learnings.jsonl")
        self._lock = threading.Lock()

    def append_learning(self, text: str, tags: list[str] | None = None) -> dict:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "text": text,
            "tags": tags or [],
        }
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def _read_all(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        entries = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return entries

    def search(self, query: str, limit: int = 5) -> list[dict]:
        query_terms = _terms(query)
        if not query_terms:
            return []
        scored = []
        for entry in self._read_all():
            text = str(entry.get("text", ""))
            score = _score(text, query_terms)
            if score > 0:
                scored.append({**entry, "score": score})
        scored.sort(key=lambda e: e["score"], reverse=True)
        return scored[:limit]

    def recent(self, limit: int = 20) -> list[dict]:
        entries = self._read_all()
        return entries[-limit:] if limit else entries

    def consolidate(self, max_entries: int = 200,
                    similarity: float = 0.55,
                    dry_run: bool = False) -> dict:
        """Merge near-duplicate learnings so the log stays compact.

        Greedy clustering by Jaccard term similarity (+0.2 when entries share
        a tag). Groups of >=2 are merged into one entry: deduplicated
        sentences, union of tags, newest timestamp.

        With ``dry_run=True`` nothing is written; the return value describes
        what *would* change (groups, before/after counts). Otherwise the
        pre-merge file is backed up to ``<path>.bak.<utc-timestamp>-<pid>``
        (pid suffix makes the name collision-safe) and the merged log is
        written atomically: temp file + fsync + ``os.replace``.

        Deterministic and LLM-free: safe to run on a schedule or from the
        ``memory.consolidate`` tool when the agent notices the log growing.
        """
        entries = self._read_all()
        before = len(entries)
        if before <= max_entries:
            return {"ok": True, "before": before, "after": before,
                    "merged": 0, "groups": 0, "backup": None,
                    "dry_run": dry_run}
        termsets = [set(_terms(str(e.get("text", "")))) for e in entries]
        tagsets = [set(str(t) for t in (e.get("tags") or [])) for e in entries]

        groups: list[list[int]] = []
        for i in range(before):
            best, best_sim = -1, 0.0
            for gi, g in enumerate(groups):
                sim = max(
                    _jaccard(termsets[i], termsets[j])
                    + (0.2 if tagsets[i] & tagsets[j] else 0.0)
                    for j in g
                )
                sim = min(sim, 1.0)
                if sim >= similarity and sim > best_sim:
                    best, best_sim = gi, sim
            if best >= 0:
                groups[best].append(i)
            else:
                groups.append([i])

        merged_entries: list[dict] = []
        merged_count = 0
        for g in groups:
            if len(g) < 2:
                merged_entries.append(entries[g[0]])
                continue
            seen: set[str] = set()
            sentences: list[str] = []
            tags: set[str] = set()
            for j in g:
                e = entries[j]
                tags.update(str(t) for t in (e.get("tags") or []))
                for s in _sentences(str(e.get("text", ""))):
                    if s not in seen:
                        seen.add(s)
                        sentences.append(s)
            sentences = sentences[:40]
            text = " ".join(sentences)[:4000]
            ts_list = [str(entries[j].get("ts", "")) for j in g if entries[j].get("ts")]
            merged_entries.append({
                "ts": max(ts_list) if ts_list else datetime.now(timezone.utc).isoformat(),
                "text": f"[consolidated {len(g)} entries] {text}",
                "tags": sorted(tags),
                "consolidated_from": len(g),
            })
            merged_count += len(g) - 1

        backup = (f"{self.path}.bak."
                  f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{os.getpid()}")
        after = len(merged_entries)
        report = {"ok": True, "before": before, "after": after,
                  "merged": merged_count,
                  "groups": sum(1 for g in groups if len(g) >= 2),
                  "backup": backup, "dry_run": dry_run}
        if dry_run:
            return report
        # Atomic rewrite: temp file + fsync + os.replace, under the lock.
        # The original is moved to the backup path first, so a crash can
        # never leave a half-written log behind.
        with self._lock:
            try:
                if os.path.exists(self.path):
                    os.replace(self.path, backup)
                else:
                    report["backup"] = None
            except OSError:
                report["backup"] = None
            tmp = f"{self.path}.tmp.{os.getpid()}"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    for e in merged_entries:
                        f.write(json.dumps(e, ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return report


# ---- module-level convenience API (what SPEC names directly) ----

_default: Memory | None = None


def _instance() -> Memory:
    global _default
    if _default is None:
        _default = Memory()
    return _default


def append_learning(text: str, tags: list[str] | None = None) -> dict:
    return _instance().append_learning(text, tags)


def search(query: str, limit: int = 5) -> list[dict]:
    return _instance().search(query, limit)


def consolidate(max_entries: int = 200, similarity: float = 0.55,
                dry_run: bool = False) -> dict:
    return _instance().consolidate(max_entries=max_entries,
                                   similarity=similarity, dry_run=dry_run)
