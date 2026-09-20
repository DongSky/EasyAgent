from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid

from .contracts import ModelRequest
from .store import encode


def terms(text):
    lowered = text.casefold()
    words = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]", lowered)
    # CJK bigrams improve substring specificity without an external tokenizer.
    return set(words + [lowered[i:i+2] for i in range(len(lowered)-1) if all('\u3400' <= c <= '\u9fff' for c in lowered[i:i+2])])


class Knowledge:
    def __init__(self, store, models):
        self.store, self.models = store, models

    async def ingest(self, namespace, title, source, text, embedding_model=None, chunk_size=800):
        if not namespace or not source or not text.strip() or len(text) > 1_000_000:
            raise ValueError("document needs namespace/source/content and must be below one million characters")
        if not 100 <= chunk_size <= 4000:
            raise ValueError("chunk size must be 100–4000 characters")
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self.store.connect() as db:
            old = db.execute("SELECT * FROM documents WHERE namespace=? AND source=?", (namespace, source)).fetchone()
            if old and old["digest"] == digest and old["embedding_model"] == embedding_model:
                return dict(old)
        chunks = [text[start:start+chunk_size] for start in range(0, len(text), chunk_size - 80)]
        if embedding_model and len(chunks) > 128:
            raise ValueError("vector ingestion is limited to 128 chunks per document; split the source")
        vectors = []
        if embedding_model:
            for chunk in chunks:
                result = await self.models.generate(ModelRequest(model=embedding_model, capability="embedding", prompt=chunk))
                if not result.embeddings or not result.embeddings[0]:
                    raise ValueError("embedding provider returned no vector")
                vectors.append(result.embeddings[0])
        identifier = uuid.uuid4().hex
        with self.store.transaction() as db:
            db.execute("DELETE FROM documents WHERE namespace=? AND source=?", (namespace, source))
            db.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?)", (identifier, namespace, title, source, digest, embedding_model, time.time()))
            for index, chunk in enumerate(chunks):
                db.execute("INSERT INTO chunks VALUES(?,?,?,?)", (identifier, index, chunk, encode(vectors[index]) if vectors else None))
        return {"id": identifier, "namespace": namespace, "title": title, "source": source, "digest": digest,
                "chunks": len(chunks), "embedding_model": embedding_model}

    async def search(self, namespace, query, limit=5, mode="lexical"):
        if mode not in ("lexical", "vector", "hybrid") or not 1 <= limit <= 50:
            raise ValueError("invalid retrieval mode or limit")
        with self.store.connect() as db:
            rows = [dict(r) for r in db.execute("""SELECT d.id,d.title,d.source,d.digest,d.embedding_model,c.position,c.content,c.embedding
                FROM documents d JOIN chunks c ON c.document_id=d.id WHERE d.namespace=?""", (namespace,))]
        query_terms = terms(query)
        query_vectors = {}
        if mode != "lexical":
            for model in {r["embedding_model"] for r in rows if r["embedding_model"]}:
                result = await self.models.generate(ModelRequest(model=model, capability="embedding", prompt=query))
                query_vectors[model] = result.embeddings[0]
            if rows and not query_vectors:
                raise ValueError("documents have no embeddings; ingest with an embedding model or use lexical search")
        results = []
        for row in rows:
            lexical = len(terms(row["content"]) & query_terms) / max(1, len(query_terms))
            vector = 0
            if row["embedding"]:
                values = json.loads(row["embedding"])
                query_vector = query_vectors.get(row["embedding_model"], [])
                if query_vector:
                    if len(values) != len(query_vector):
                        raise ValueError("embedding dimensions changed; re-ingest the document")
                    norm = math.sqrt(sum(x*x for x in values) * sum(x*x for x in query_vector))
                    vector = sum(a*b for a, b in zip(values, query_vector)) / norm if norm else 0
            score = lexical if mode == "lexical" else vector if mode == "vector" else (lexical + vector) / 2
            if score > 0:
                results.append({k: v for k, v in row.items() if k not in ("embedding", "embedding_model")} | {"score": score})
        return sorted(results, key=lambda r: r["score"], reverse=True)[:limit]

    def list(self, namespace=None):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM documents WHERE (? IS NULL OR namespace=?) ORDER BY created DESC", (namespace, namespace))]

    def delete(self, identifier):
        with self.store.connect() as db:
            db.execute("DELETE FROM documents WHERE id=?", (identifier,))
