from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from pathlib import Path
from typing import Any

import numpy as np
import requests

from system.config import settings

LOGGER = logging.getLogger(__name__)

try:
    import faiss
except ImportError:
    faiss = None


MEMORY_QUERY_PATTERNS = [
    r"\bwhat did (i|we) (.+?) (talk|discuss|ask|mention|remember)",
    r"\bdo you remember (when|i|me|us)",
    r"\bhave we (talked|discussed|spoken) about",
    r"\bsearch (my|our)? ?conversation",
    r"\bwhat (was|were) we (talking|discussing) about",
    r"\bpast conversations",
    r"\bprevious (conversation|chat|discussion)",
    r"\brecall",
    r"\bearlier.*(you|told|said|did)",
    r"\bwhat.*you.*know.*about.*me",
    r"\bhow long ago.*we.*talk",
    r"\bwhen.*i.*ask.*you.*about",
]


class EmbeddingService:
    """Calls Ollama /api/embeddings endpoint for local embeddings."""

    def __init__(self, base_url: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._dimension: int | None = None

    @property
    def dimension(self) -> int:
        if self._dimension is None:
            self._dimension = self._detect_dimension()
        return self._dimension

    def _detect_dimension(self) -> int:
        try:
            resp = requests.post(
                f"{self.base_url}/api/embeddings",
                json={"model": self.model, "prompt": "dimension test"},
                timeout=30,
            )
            resp.raise_for_status()
            embedding = resp.json().get("embedding", [])
            return len(embedding)
        except Exception:
            LOGGER.warning("Could not detect embedding dimension, using default 4096")
            return 4096

    def embed(self, texts: list[str]) -> list[np.ndarray]:
        """Returns normalized embedding vectors via Ollama /api/embeddings."""
        embeddings: list[np.ndarray] = []
        for text in texts:
            payload = {"model": self.model, "prompt": text}
            resp = requests.post(f"{self.base_url}/api/embeddings", json=payload, timeout=60)
            resp.raise_for_status()
            vec = np.array(resp.json()["embedding"], dtype=np.float32)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            embeddings.append(vec)
        return embeddings


class Chunker:
    """Groups conversation turns into overlapping text chunks."""

    def __init__(self, turns_per_chunk: int = 3, overlap: int = 1) -> None:
        self.turns_per_chunk = turns_per_chunk
        self.overlap = overlap

    def chunk_turns(self, turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Yields chunks with metadata, from a list of ConversationTurn dicts."""
        if not turns:
            return []

        chunks: list[dict[str, Any]] = []
        i = 0
        while i <= len(turns) - self.turns_per_chunk:
            group = turns[i : i + self.turns_per_chunk]
            text = "\n".join(
                f"{t['role']}: {t['text']}" for t in group if t.get("text")
            )
            if text.strip():
                chunks.append(
                    {
                        "chunk_id": str(uuid.uuid4()),
                        "text": text,
                        "created_at": group[0].get("created_at", ""),
                        "role": group[0].get("role", "unknown"),
                        "kind": group[0].get("kind", "conversation"),
                    }
                )
            i += self.turns_per_chunk - self.overlap

        if i < len(turns):
            remaining = turns[i:]
            text = "\n".join(
                f"{t['role']}: {t['text']}" for t in remaining if t.get("text")
            )
            if text.strip():
                chunks.append(
                    {
                        "chunk_id": str(uuid.uuid4()),
                        "text": text,
                        "created_at": remaining[0].get("created_at", ""),
                        "role": remaining[0].get("role", "unknown"),
                        "kind": remaining[0].get("kind", "conversation"),
                    }
                )
        return chunks


class VectorStore:
    """FAISS-backed vector store with metadata."""

    def __init__(self, index_dir: Path, dimension: int = 4096) -> None:
        self.index_dir = index_dir
        self.dimension = dimension
        self.index_path = index_dir / "index.faiss"
        self.meta_path = index_dir / "metadata.json"
        self._lock = threading.RLock()
        self._index: Any = None
        self._metadata: list[dict[str, Any]] = []

    def _ensure_index(self) -> None:
        if self._index is not None:
            return
        if faiss is None:
            raise RuntimeError("faiss-cpu is not installed")
        self.index_dir.mkdir(parents=True, exist_ok=True)
        if self.index_path.exists():
            try:
                self._index = faiss.read_index(str(self.index_path))
                with open(self.meta_path, "r", encoding="utf-8") as f:
                    self._metadata = json.load(f)
            except Exception:
                LOGGER.warning("Failed to load FAISS index, creating fresh one")
                self._index = faiss.IndexIDMap(faiss.IndexFlatIP(self.dimension))
                self._metadata = []
        else:
            self._index = faiss.IndexIDMap(faiss.IndexFlatIP(self.dimension))

    def add(self, embeddings: np.ndarray, chunks: list[dict[str, Any]]) -> None:
        with self._lock:
            self._ensure_index()
            ids = np.array(
                [hash(c["chunk_id"]) % (2**63) for c in chunks], dtype=np.int64
            )
            self._index.add_with_ids(embeddings.astype(np.float32), ids)
            self._metadata.extend(chunks)
            self._save()

    def search(
        self, query_embedding: np.ndarray, top_k: int = 5
    ) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_index()
            if self._index.ntotal == 0:
                return []
            query_embedding = query_embedding.astype(np.float32).reshape(1, -1)
            distances, indices = self._index.search(
                query_embedding, min(top_k, self._index.ntotal)
            )
            results = []
            for dist, idx in zip(distances[0], indices[0]):
                if 0 <= idx < len(self._metadata):
                    result = dict(self._metadata[idx])
                    result["score"] = float(dist)
                    results.append(result)
            return results

    def _save(self) -> None:
        with self._lock:
            if self._index is None:
                return
            faiss.write_index(self._index, str(self.index_path))
            with open(self.meta_path, "w", encoding="utf-8") as f:
                json.dump(self._metadata, f, indent=2)


class SemanticMemory:
    """Main semantic memory interface."""

    def __init__(
        self,
        index_path: Path,
        ollama_base_url: str,
        model: str,
        turns_per_chunk: int = 3,
        overlap: int = 1,
    ) -> None:
        self.embedder = EmbeddingService(ollama_base_url, model)
        self.chunker = Chunker(turns_per_chunk=turns_per_chunk, overlap=overlap)
        self.store = VectorStore(index_path, dimension=self.embedder.dimension)
        self._healthy = True

    def health_check(self) -> bool:
        if faiss is None:
            return False
        try:
            dummy = np.zeros(self.embedder.dimension, dtype=np.float32)
            self.store.search(dummy, top_k=1)
            return True
        except Exception:
            return False

    def index_exchange(
        self,
        user_text: str,
        assistant_text: str,
        kind: str,
        created_at: str,
    ) -> None:
        turns: list[dict[str, Any]] = []
        if user_text.strip():
            turns.append(
                {
                    "role": "user",
                    "text": user_text.strip(),
                    "kind": kind,
                    "created_at": created_at,
                }
            )
        if assistant_text.strip():
            turns.append(
                {
                    "role": "assistant",
                    "text": assistant_text.strip(),
                    "kind": kind,
                    "created_at": created_at,
                }
            )

        if not turns:
            return

        chunks = self.chunker.chunk_turns(turns)
        if not chunks:
            return

        texts = [c["text"] for c in chunks]
        try:
            embeddings = self.embedder.embed(texts)
            self.store.add(np.stack(embeddings), chunks)
        except requests.RequestException as exc:
            LOGGER.warning(
                "Ollama embeddings unavailable, skipping semantic indexing: %s", exc
            )
            self._healthy = False

    def search(
        self, query: str, top_k: int = 5, role_filter: str | None = None
    ) -> list[dict[str, Any]]:
        try:
            embeddings = self.embedder.embed([query])
            results = self.store.search(embeddings[0], top_k=top_k)
            if role_filter:
                results = [r for r in results if r.get("role") == role_filter]
            return results
        except Exception as exc:
            LOGGER.warning("Semantic search failed, returning empty: %s", exc)
            return []

    def is_memory_query(self, text: str) -> bool:
        text_lower = text.lower().strip()
        return any(re.search(pattern, text_lower) for pattern in MEMORY_QUERY_PATTERNS)

    def answer_query(self, query: str, top_k: int = 5) -> str | None:
        if not self.is_memory_query(query):
            return None
        results = self.search(query, top_k=top_k)
        if not results:
            return None
        lines = []
        for r in results:
            role = r.get("role", "unknown")
            text = r.get("text", "")
            time = r.get("created_at", "")[:10]
            lines.append(f"[{time}] {role}: {text[:300]}")
        return "From your conversation history: " + " | ".join(lines[:3])

    def rebuild_index(self, turns: list[dict[str, Any]]) -> int:
        """Full rebuild from a list of conversation turns."""
        chunks = self.chunker.chunk_turns(turns)
        if not chunks:
            return 0
        texts = [c["text"] for c in chunks]
        embeddings = self.embedder.embed(texts)
        self.store.add(np.stack(embeddings), chunks)
        return len(chunks)
