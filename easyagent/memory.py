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
