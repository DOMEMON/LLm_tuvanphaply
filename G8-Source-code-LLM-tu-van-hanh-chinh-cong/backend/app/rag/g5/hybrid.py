"""Optional dense+lexical procedure router with a fail-safe lexical fallback."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.rag.catalog_policy import is_serving_candidate
from app.rag.g3.retrieval import bm25, procedure_names, routing_query
from app.rag.retrieval import normalize
from app.rag.g5.text_views import surface

DEFAULT_ALLOWED_HOSTS = frozenset(
    {"127.0.0.1", "::1", "embedding-server", "host.docker.internal", "localhost"}
)


@dataclass(frozen=True, slots=True)
class RouteCandidate:
    procedure_id: str
    score: float
    lexical_score: float
    dense_score: float


class HybridProcedureRouter:
    """Route only when the hybrid winner is both strong and unambiguous.

    An outage or uncertain dense result returns ``None``. The caller then uses
    the existing exact/BM25 route; an embedding service can never make the RAG
    endpoint unavailable.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model_name: str,
        timeout_seconds: float = 5,
        dense_weight: float = 0.55,
        min_score: float = 0.52,
        min_margin: float = 0.04,
        allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_HOSTS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname.casefold() if parsed.hostname else None
        if (
            parsed.scheme != "http"
            or hostname not in {item.casefold() for item in allowed_hosts}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("G5_EMBEDDING_URL_NOT_ALLOWED")
        if not model_name.strip() or not 0 <= dense_weight <= 1:
            raise ValueError("G5_HYBRID_CONFIG_INVALID")
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.dense_weight = dense_weight
        self.min_score = min_score
        self.min_margin = min_margin
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None
        self._document_cache: dict[str, tuple[list[str], list[list[float]]]] = {}
        self._semantic_cache: dict[str, tuple[list[str], list[list[float]]]] = {}
        self._cache_lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _embed(self, values: list[str]) -> list[list[float]]:
        response = await self._client.post(
            f"{self.base_url}/embeddings",
            json={"model": self.model_name, "input": values, "encoding_format": "float"},
        )
        response.raise_for_status()
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or len(data) != len(values):
            raise ValueError("G5_EMBEDDING_RESPONSE_INVALID")
        ordered = sorted(data, key=lambda item: item.get("index", -1))
        vectors = [item.get("embedding") for item in ordered]
        if any(
            not isinstance(vector, list)
            or not vector
            or any(
                not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector
            )
            for vector in vectors
        ):
            raise ValueError("G5_EMBEDDING_VECTOR_INVALID")
        dimensions = {len(vector) for vector in vectors}
        if len(dimensions) != 1:
            raise ValueError("G5_EMBEDDING_DIMENSION_MISMATCH")
        return vectors

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if len(left) != len(right):
            raise ValueError("G5_EMBEDDING_DIMENSION_MISMATCH")
        denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(
            sum(value * value for value in right)
        )
        return sum(a * b for a, b in zip(left, right)) / denominator if denominator else 0.0

    async def _documents(self, data) -> tuple[list[str], list[str], list[list[float]]]:
        procedure_ids = sorted(
            procedure_id
            for procedure_id in data.procedures
            if is_serving_candidate(procedure_id)
        )
        texts = [
            " | ".join(
                [
                    data.procedures[procedure_id]["title"],
                    *data.procedures[procedure_id].get("aliases", []),
                    data.procedures[procedure_id]["category_label"],
                ]
            )
            for procedure_id in procedure_ids
        ]
        cache_key = data.fingerprint
        cached = self._document_cache.get(cache_key)
        if cached and cached[0] == texts:
            return procedure_ids, texts, cached[1]
        async with self._cache_lock:
            cached = self._document_cache.get(cache_key)
            if cached and cached[0] == texts:
                return procedure_ids, texts, cached[1]
            vectors = await self._embed(texts)
            self._document_cache = {cache_key: (texts, vectors)}
            return procedure_ids, texts, vectors

    async def route(self, data, query: str) -> RouteCandidate | None:
        normalized_query = f" {normalize(query)} "
        if any(
            f" {name} " in normalized_query
            for procedure in data.procedures.values()
            for name in procedure_names(data, procedure)
        ):
            # Exact/alias/code routing is deterministic and cheaper. Dense routing
            # is reserved for semantic wording that the lexical path cannot name.
            return None
        scoped_query = routing_query(query)
        if len(scoped_query.split()) < 2:
            return None
        try:
            procedure_ids, texts, document_vectors = await self._documents(data)
            query_text = (
                "Instruct: Chọn thủ tục hành chính phù hợp nhất cho câu hỏi tiếng Việt.\n"
                f"Query: {surface(query)}"
            )
            query_vector = (await self._embed([query_text]))[0]
        except (httpx.HTTPError, ValueError, RuntimeError):
            return None

        lexical = bm25(scoped_query, [normalize(text) for text in texts])
        lexical_max = max(lexical, default=0) or 1
        candidates = []
        for procedure_id, sparse, dense_vector in zip(procedure_ids, lexical, document_vectors):
            dense = max(0.0, self._cosine(query_vector, dense_vector))
            sparse_normalized = sparse / lexical_max
            score = self.dense_weight * dense + (1 - self.dense_weight) * sparse_normalized
            candidates.append(RouteCandidate(procedure_id, score, sparse_normalized, dense))
        candidates.sort(key=lambda item: (-item.score, item.procedure_id))
        if not candidates or candidates[0].score < self.min_score:
            return None
        if len(candidates) > 1 and candidates[0].score - candidates[1].score < self.min_margin:
            return None
        return candidates[0]

    async def semantic_candidates(self, data, query: str) -> list[RouteCandidate]:
        """Independent meaning-based suggestions, never authority to answer.

        Preserve Vietnamese accents and keep this separate from normalized BM25.
        The caller must require model agreement and citizen confirmation.
        """
        ids = sorted(pid for pid in data.accepted if is_serving_candidate(pid))
        texts = [data.procedures[pid]["title"] + " | "
                 + data.procedures[pid].get("category_label", "") for pid in ids]
        try:
            async with self._cache_lock:
                cached = self._semantic_cache.get(data.fingerprint)
                if not cached or cached[0] != texts:
                    cached = (texts, await self._embed(texts))
                    self._semantic_cache = {data.fingerprint: cached}
            vector = (await self._embed([
                "Instruct: Chọn thủ tục hành chính phù hợp nhất cho câu hỏi tiếng Việt.\n"
                f"Query: {query[:4000]}"
            ]))[0]
            candidates = []
            for pid, document in zip(ids, cached[1], strict=True):
                score = self._cosine(vector, document)
                candidates.append(RouteCandidate(pid, score, 0.0, score))
            return sorted(candidates, key=lambda item: (-item.dense_score, item.procedure_id))[:3]
        except (httpx.HTTPError, ValueError, RuntimeError):
            return []
