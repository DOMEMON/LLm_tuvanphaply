"""Domain-neutral evidence index; no procedural IDs, field enums or locality rules."""
from dataclasses import dataclass, field
from collections import Counter
import math
import re
import unicodedata


def tokens(text):
    folded = "".join(c for c in unicodedata.normalize("NFD", text.casefold())
                     if unicodedata.category(c) != "Mn").replace("đ", "d")
    stop = {"toi", "minh", "ban", "cho", "va", "la", "cua", "nhung", "cac", "mot", "nay", "thi", "gi"}
    return [t for t in re.findall(r"\w+", folded) if t not in stop]


@dataclass
class Passage:
    id: str
    source_id: str
    text: str
    title: str
    entity: str = ""
    attribute: str = ""
    url: str | None = None
    metadata: dict = field(default_factory=dict)

    def citation(self):
        return {"fragment_id": self.id, "source_id": self.source_id,
                "title": self.title, "url": self.url, "metadata": self.metadata}


class Package:
    def __init__(self, profile, version, catalog, domains, boundaries, passages, direct, policy=None, catalog_config=None, preflight=None):
        self.profile, self.version = profile, version
        self.catalog, self.domains, self.boundaries = catalog, domains, boundaries
        self.passages, self.direct = passages, direct
        self.policy = policy
        self.catalog_config = catalog_config or {}
        self.preflight = preflight
        self.counts = [Counter(tokens(p.title + " " + p.text)) for p in passages]
        self.df = Counter(t for terms in self.counts for t in terms)
        self.avg_length = sum(sum(c.values()) for c in self.counts) / max(1, len(self.counts))

    @staticmethod
    def valid_at(passage, as_of=None):
        if not as_of:
            return True
        date_text = as_of.isoformat()
        return not (passage.metadata.get('effective_from') and date_text < passage.metadata['effective_from']
                    or passage.metadata.get('effective_to') and date_text > passage.metadata['effective_to'])

    def search(self, query, entity="", attributes=(), top_k=6, as_of=None):
        terms = set(tokens(query))
        ranked = []
        n = len(self.passages)
        for passage, counts in zip(self.passages, self.counts):
            if entity and passage.entity != entity or attributes and passage.attribute not in attributes:
                continue
            if not self.valid_at(passage, as_of):
                continue
            length, score = sum(counts.values()), 0
            for term in terms:
                tf = counts[term]
                if tf:
                    idf = math.log(1 + (n - self.df[term] + .5) / (self.df[term] + .5))
                    score += idf * tf * 2.2 / (tf + 1.2 * (.25 + .75 * length / max(1, self.avg_length)))
            if score > 0:
                ranked.append((score, passage))
        ranked.sort(key=lambda row: (-row[0], row[1].id))
        return [p for _, p in ranked[:top_k]]
