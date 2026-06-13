"""Semantic memory store: LanceDB + nomic-embed-text-v1 (CUDA).

Embeds each episode's summary text. Retrieval is k-NN over the focus query.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import lancedb
import numpy as np
import pyarrow as pa

logger = logging.getLogger(__name__)

_MODEL_NAME = "nomic-ai/nomic-embed-text-v1"
_EMBED_DIM = 768  # nomic-embed-text-v1 is 768-dim
_TABLE = "episode_memory"


class SemanticStore:
    """LanceDB-backed semantic store using nomic-embed on CUDA."""

    def __init__(self, db_path: str | Path, device: str | None = None):
        self.db_path = str(db_path)
        Path(self.db_path).mkdir(parents=True, exist_ok=True)
        self._db = lancedb.connect(self.db_path)
        self._device = device or self._auto_device()
        self._model = None  # lazy
        self._ensure_table()

    @staticmethod
    def _auto_device() -> str:
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
        except Exception:
            pass
        return "cpu"

    def _load_model(self):
        if self._model is not None:
            return self._model
        from sentence_transformers import SentenceTransformer
        logger.info("Loading %s on %s", _MODEL_NAME, self._device)
        self._model = SentenceTransformer(
            _MODEL_NAME, trust_remote_code=True, device=self._device
        )
        return self._model

    def _ensure_table(self) -> None:
        if _TABLE in self._db.table_names():
            return
        schema = pa.schema(
            [
                pa.field("episode_id", pa.int64()),
                pa.field("invocation_num", pa.int64()),
                pa.field("timestamp", pa.string()),
                pa.field("kind", pa.string()),          # 'episode' or 'weekly_summary'
                pa.field("text", pa.string()),
                pa.field(
                    "vector",
                    pa.list_(pa.float32(), list_size=_EMBED_DIM),
                ),
            ]
        )
        self._db.create_table(_TABLE, schema=schema)

    @property
    def table(self):
        return self._db.open_table(_TABLE)

    def embed(self, texts: list[str], *, kind: str) -> np.ndarray:
        """Encode with the nomic-specified prefix.

        nomic-embed-text-v1 expects 'search_document: ' for stored items and
        'search_query: ' for queries.
        """
        model = self._load_model()
        prefix = "search_query: " if kind == "query" else "search_document: "
        prefixed = [prefix + t for t in texts]
        vecs = model.encode(prefixed, normalize_embeddings=True, convert_to_numpy=True)
        return vecs.astype(np.float32)

    def add_episode(
        self,
        *,
        episode_id: int,
        invocation_num: int,
        timestamp: str,
        text: str,
        kind: str = "episode",
    ) -> None:
        if not text or not text.strip():
            return
        vec = self.embed([text], kind="document")[0]
        self.table.add(
            [
                {
                    "episode_id": int(episode_id),
                    "invocation_num": int(invocation_num),
                    "timestamp": timestamp,
                    "kind": kind,
                    "text": text,
                    "vector": vec.tolist(),
                }
            ]
        )

    def search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        if not query or not query.strip():
            return []
        try:
            count = self.table.count_rows()
        except Exception:
            count = 0
        if count == 0:
            return []
        vec = self.embed([query], kind="query")[0]
        results = (
            self.table.search(vec.tolist())
            .limit(min(k, count))
            .to_list()
        )
        return results

    def count(self) -> int:
        try:
            return int(self.table.count_rows())
        except Exception:
            return 0


def summarize_episode_for_embedding(episode: dict[str, Any]) -> str:
    """Compact textual representation of an episode used for embedding + retrieval."""
    parts: list[str] = []
    inv = episode.get("invocation_num")
    ts = episode.get("timestamp")
    if inv is not None:
        parts.append(f"Invocation {inv} at {ts}.")
    focus = episode.get("current_focus")
    if focus:
        parts.append(f"Focus: {focus}.")
    actions = episode.get("actions_taken") or []
    if actions:
        parts.append("Actions: " + "; ".join(str(a) for a in actions))
    decisions = episode.get("decisions_made")
    if decisions:
        parts.append(f"Decisions: {decisions}")
    state = episode.get("internal_state")
    if state:
        parts.append(f"Internal state: {state}")
    journal = episode.get("journal_entry")
    if journal:
        parts.append(f"Journal: {journal}")
    return "\n".join(parts).strip()


if __name__ == "__main__":
    import sys
    store = SemanticStore(sys.argv[1] if len(sys.argv) > 1 else "data/vectors")
    print(f"Device: {store._device}")
    print(f"Table rows: {store.count()}")
    print("Encoding test query...")
    v = store.embed(["this is a test"], kind="query")
    print(f"Vector shape: {v.shape}, norm: {float(np.linalg.norm(v[0])):.4f}")
