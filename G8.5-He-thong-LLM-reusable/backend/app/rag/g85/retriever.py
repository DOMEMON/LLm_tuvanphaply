"""LangChain retriever adapter; facts remain in the immutable package."""
from typing import Any

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever


class PackageRetriever(BaseRetriever):
    package: Any
    entity: str = ''
    attributes: tuple[str, ...] = ()
    top_k: int = 6
    as_of: Any = None

    def _get_relevant_documents(self, query, *, run_manager):
        passages = self.package.search(query, self.entity, self.attributes, self.top_k, self.as_of)
        # A validated named entity already identifies the document collection.
        # Lexical zero (e.g. an inflected synonym) must not erase that collection.
        # This is candidate evidence only: the selector still must answer/abstain.
        if not passages and self.entity in self.package.catalog:
            passages = [p for p in self.package.passages if p.entity == self.entity
                and (not self.attributes or p.attribute in self.attributes)
                and self.package.valid_at(p, self.as_of)][:self.top_k]
        return [Document(page_content=p.text, metadata={'passage_id': p.id, 'source_id': p.source_id})
                for p in passages]


def retrieve(package, query, entity='', attributes=(), top_k=6, as_of=None):
    documents = PackageRetriever(package=package, entity=entity, attributes=tuple(attributes),
        top_k=top_k, as_of=as_of).invoke(query, config={'callbacks': []})
    by_id = {p.id: p for p in package.passages}
    return [by_id[d.metadata['passage_id']] for d in documents]
