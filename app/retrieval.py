"""Document retrieval with source authority baked into ranking.

Deliberately a dependency-free BM25 over heading-level chunks:

* the corpus is six short documents - an embedding index would add a network
  hop, a model download and a similarity threshold to tune, and would still
  need the same authority/permission layer on top;
* lexical match is exactly right for policy lookups, where the query and the
  clause share vocabulary ("cancellation fee", "service credit", "bulk upload");
* it makes retrieval fully deterministic and unit-testable, which matters more
  here than semantic recall.

Ranking = BM25 * authority weight. Deprecated documents are filtered out
unless explicitly requested, and confidential contracts are filtered by the
caller's account scope, in the data layer - not by prompt instruction.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

from .loaders import Document

TOKEN_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = {
    "the", "a", "an", "of", "for", "to", "and", "or", "is", "are", "in", "on", "at", "be",
    "can", "do", "does", "we", "i", "it", "this", "that", "with", "what", "how", "when",
    "if", "was", "were", "there", "our", "my", "you", "your",
}

AUTHORITY_WEIGHT = {1: 1.35, 2: 1.20, 3: 1.00, 4: 0.70, 5: 0.35}


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN_RE.findall(text.lower()) if t not in STOPWORDS and len(t) > 1]


@dataclass
class Chunk:
    chunk_id: str
    doc: Document
    heading: str
    text: str
    tokens: list[str]

    @property
    def citation(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc.doc_id,
            "title": self.doc.title,
            "clause": self.heading,
            "doc_type": self.doc.doc_type,
            "status": self.doc.status,
            "effective_date": self.doc.effective_date,
            "authority_tier": self.doc.authority_tier,
            "source_file": self.doc.source_file,
        }


def chunk_document(doc: Document) -> list[Chunk]:
    """Split on markdown headings; each clause becomes one citable chunk."""
    lines = doc.text.splitlines()
    chunks: list[Chunk] = []
    heading = "Preamble"
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            idx = len(chunks)
            chunks.append(
                Chunk(
                    chunk_id=f"{doc.doc_id}#{idx}",
                    doc=doc,
                    heading=heading,
                    text=body,
                    tokens=tokenize(f"{doc.title} {heading} {body}"),
                )
            )

    for line in lines:
        if line.startswith("#"):
            flush()
            buffer = []
            heading = line.lstrip("#").strip()
        else:
            buffer.append(line)
    flush()
    return chunks


class DocumentIndex:
    """BM25 index with authority-aware scoring and hard permission filters."""

    k1 = 1.5
    b = 0.75

    def __init__(self, documents: Iterable[Document]):
        self.documents = list(documents)
        self.chunks: list[Chunk] = []
        for doc in self.documents:
            self.chunks.extend(chunk_document(doc))
        self.doc_freq: Counter[str] = Counter()
        for chunk in self.chunks:
            for term in set(chunk.tokens):
                self.doc_freq[term] += 1
        self.n = max(1, len(self.chunks))
        self.avg_len = sum(len(c.tokens) for c in self.chunks) / self.n

    def _bm25(self, query_tokens: list[str], chunk: Chunk) -> float:
        if not chunk.tokens:
            return 0.0
        counts = Counter(chunk.tokens)
        length = len(chunk.tokens)
        score = 0.0
        for term in query_tokens:
            tf = counts.get(term, 0)
            if not tf:
                continue
            idf = math.log(1 + (self.n - self.doc_freq[term] + 0.5) / (self.doc_freq[term] + 0.5))
            score += idf * (tf * (self.k1 + 1)) / (tf + self.k1 * (1 - self.b + self.b * length / self.avg_len))
        return score

    def search(
        self,
        query: str,
        *,
        allowed_account_ids: set[str] | None = None,
        allow_confidential: bool = True,
        doc_types: list[str] | None = None,
        include_deprecated: bool = False,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        query_tokens = tokenize(query)
        results: list[dict[str, Any]] = []

        for chunk in self.chunks:
            doc = chunk.doc
            if doc.is_deprecated and not include_deprecated:
                continue
            if doc_types and doc.doc_type not in doc_types:
                continue
            if doc.confidential and not allow_confidential:
                continue
            if doc.account_id and allowed_account_ids is not None and doc.account_id not in allowed_account_ids:
                continue  # contract for an account outside the caller's scope

            base = self._bm25(query_tokens, chunk)
            if base <= 0:
                continue
            weight = AUTHORITY_WEIGHT.get(doc.authority_tier, 1.0)
            results.append(
                {
                    "chunk_id": chunk.chunk_id,
                    "score": round(base * weight, 4),
                    "bm25": round(base, 4),
                    "authority_weight": weight,
                    "text": chunk.text,
                    **chunk.citation,
                    "warning": (
                        f"DEPRECATED source - superseded by {doc.superseded_by}. Never quote as current policy."
                        if doc.is_deprecated
                        else None
                    ),
                }
            )

        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:limit]

    def get_document(self, doc_id: str) -> Document | None:
        for doc in self.documents:
            if doc.doc_id == doc_id:
                return doc
        return None
